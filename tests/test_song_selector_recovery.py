import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.autoslice import song_selector_recovery as recovery


def test_saved_service_failure_keeps_real_backoff_across_new_entry_and_output_directory(tmp_path, monkeypatch):
    source=tmp_path/'source.mp4';source.write_bytes(b'bound complete source')
    record={'candidate_id':'song','decision_action':'BLOCK','reason_codes':['AGY_AND_GEMINI_API_FAILED']}
    summary=tmp_path/'summary.json';summary.write_text(json.dumps({'source_video':str(source),'records':[record]}))
    before=summary.read_bytes();monkeypatch.setattr(recovery.time,'time',lambda:1000)
    with recovery.selector_recovery(source,'song',10,20) as state:
        state.adopt_completed_failure(summary,source)
        first=state.result_record();path=state.path
        assert first['next_retry_at_epoch']==1900 and first['transient_retry_count']==1
        assert state.resume_record(now=1899)['next_retry_at']==first['next_retry_at']
    with recovery.selector_recovery(source,'song',10,20) as state:
        assert state.path==path and state.resume_record(now=1900) is None
        state.start();second=state.finish(record,now=1901)
        assert second['transient_retry_count']==2 and second['next_retry_at_epoch']==3701
        complete=state.finish({**record,'reason_codes':[],'decision_action':'AUTO_UPLOAD'},now=3702)
        assert 'next_retry_at_epoch' not in complete and complete['transient_retry_count']==2
    assert summary.read_bytes()==before and not (tmp_path/'state').exists()


def test_interrupted_selector_does_not_silently_redispatch_an_unknown_outcome(tmp_path):
    source=tmp_path/'source.mp4';source.write_bytes(b'bound source')
    with recovery.selector_recovery(source,'song',10,20) as state:
        state.start();path=state.path
    with recovery.selector_recovery(source,'song',10,20) as state:
        assert state.resume_record()['reason_codes']==['CALL_OUTCOME_UNKNOWN']
    frozen=path.read_bytes();source.write_bytes(b'a genuinely different source')
    with recovery.selector_recovery(source,'song',10,20) as state:
        assert state.path!=path and state.resume_record() is None
    assert path.read_bytes()==frozen


def test_ordinary_selector_waits_before_cpa_and_audio_dispatch_and_records_the_real_stage(tmp_path, monkeypatch):
    from scripts import run_full_session_selector_cpa_shadow as selector
    from src.autoslice.production_attempts import read_attempts
    from src.autoslice.cpa_runtime import record_cpa_event
    root=tmp_path/'2026-10-12';root.mkdir();source=root/'song.mp4';source.write_bytes(b'complete source')
    srt=root/'source.srt';srt.write_text('1\n00:00:00,000 --> 00:00:01,000\n歌词\n')
    out=root/'selector';out.mkdir()
    args=SimpleNamespace(source_srt=srt,source_video=source,output_dir=out,max_candidates=1)
    candidate=SimpleNamespace(anchor=SimpleNamespace(candidate_id='song',anchor_start_ms=10,anchor_end_ms=20),content_type_hint='song')
    monkeypatch.setenv('AUTOSLICE_BASE',str(tmp_path))
    monkeypatch.setattr(selector,'load_discovered_term_lexicon',lambda *_: {})
    monkeypatch.setattr(selector,'_build_lrc_provider',lambda *_: None)
    monkeypatch.setattr(recovery.time,'time',lambda:1000)
    calls=[]
    def observed_failure(**kwargs):
        calls.append(kwargs)
        record_cpa_event('model_request',{'kind':'text'})
        record_cpa_event('http_attempt',{'kind':'text','status':200})
        record_cpa_event('audio_model_request',{'status':503})
        record_cpa_event('audio_http_attempt',{'status':503})
        return {'candidate_id':'song','decision_action':'BLOCK','reason_codes':['AGY_AND_GEMINI_API_FAILED']},{}
    monkeypatch.setattr(selector,'_review_one_selected_candidate',observed_failure)
    def run():return selector._review_selected_candidates(args=args,candidates=[candidate],cues=[],source_duration_ms=100,
        selector_stage='seeded_song_anchor',danmaku_items=[],host_vocal_prover=None,branding_intro=None)
    rows,_=run();again,_=run()
    assert len(calls)==1 and again[0]['next_retry_at_epoch']==rows[0]['next_retry_at_epoch']==1900
    attempts=read_attempts(tmp_path,'2026-10-12')
    assert [r['status'] for r in attempts]==['INFRASTRUCTURE_WAIT','INFRASTRUCTURE_WAIT']
    assert attempts[0]['model_requests_observed']==attempts[0]['audio_http_attempts_observed']==1
    assert attempts[1]['model_requests_observed']==0 and attempts[1]['cache_hits_observed']==1
    report=(tmp_path/'reports/latest.md').read_text()
    assert '服务待恢复 1' in report and '阻断 0' in report and '下次' in report
    assert not (tmp_path/'state').exists()
