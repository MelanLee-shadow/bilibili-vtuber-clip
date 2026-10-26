"""Synthetic words only; no real lyric or ASR corpus belongs in these fixtures."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import pytest

from src.autoslice.review_evidence import SourceCue
from src.autoslice.song_common import LrcLine, LrcResult
from src.autoslice.song_alignment import (
    AsrTimelinePending, build_asr_anchored_timeline, verify_timing_report,
)
from src.autoslice.song_performance import _validated_audio_lrc_selection, LivePerformanceRejected
from src.autoslice.song_repair import _materialize_song_repair, attempt_song_repair
from src.autoslice.recut_materialization import _load_lyric_timeline, _write_lyric_timeline_srt
from test_song_repair import _write_fake_audio_alignment_run, _rebind_fake_audio_alignment_run

WORDS = ['amber lantern', 'cobalt river', 'violet meadow', 'silver willow',
         'scarlet comet', 'golden sparrow', 'velvet orchard', 'crimson planet',
         'indigo forest', 'copper ocean', 'marble thunder', 'saffron valley']


def fixture(count=12, step=6000, offset=40000, speed=1.0):
    lines = [LrcLine(10000+i*step, WORDS[i % len(WORDS)] + chr(0x4e00+i)) for i in range(count)]
    cues = [SourceCue(str(i), round(l.time_ms*speed)+offset,
                      round(l.time_ms*speed)+offset+3000, l.text) for i,l in enumerate(lines)]
    return lines, cues


def test_constant_offset_uses_one_section():
    lines,cues=fixture()
    t=build_asr_anchored_timeline(lines,cues,source_duration_ms=200000)
    assert t.offset_ms==40000 and len(t.sections)==1
    assert t.anchor_count==12 and t.coverage==1
    assert t.sections[0].max_residual_ms==0


@pytest.mark.parametrize("tail_residual, shift", [(2000, 500), (-2000, -500), (3000, 1500)])
def test_feasible_common_shift_retains_skewed_endpoint(tail_residual, shift):
    lines,cues=fixture(5)
    cues[-1]=replace(cues[-1],source_start_ms=cues[-1].source_start_ms+tail_residual,
                     source_end_ms=cues[-1].source_end_ms+tail_residual)
    t=build_asr_anchored_timeline(lines,cues,source_duration_ms=200000)
    assert t.anchor_count==5 and t.dropped_anchor_count==0
    assert len(t.sections)==1 and t.offset_ms==40000+shift
    assert t.sections[0].max_residual_ms<=1500
    assert all(abs(p.live_start_ms-c.source_start_ms)<=1500 for p,c in zip(t.lines,cues))


def test_no_feasible_three_anchor_section_stays_pending():
    lines,cues=fixture(5)
    cues=[replace(c,source_start_ms=c.source_start_ms+i*4000,
                  source_end_ms=c.source_end_ms+i*4000) for i,c in enumerate(cues)]
    with pytest.raises(AsrTimelinePending,match='fit no constant-shift section'):
        build_asr_anchored_timeline(lines,cues,source_duration_ms=200000)


def test_section_boundary_shifts_stay_feasible_and_in_live_order():
    residuals=[22630,21400,20290,19610,18910,18570,18210,17740,17620,17350,16800]
    lines,_=fixture(len(residuals))
    lines=[replace(line,time_ms=(10000+i*6000 if i<3 else 25560+(i-3)*6000))
           for i,line in enumerate(lines)]
    cues=[SourceCue(str(i),line.time_ms+r,line.time_ms+r+3000,line.text)
          for i,(line,r) in enumerate(zip(lines,residuals))]
    t=build_asr_anchored_timeline(lines,cues,source_duration_ms=200000)
    assert t.anchor_count==len(lines) and t.dropped_anchor_count==0
    assert [s.shift_ms for s in t.sections]==[21400,18140]
    assert all(s.max_residual_ms<=1500 for s in t.sections)
    assert all(b.live_start_ms-a.live_start_ms>=300 for a,b in zip(t.lines,t.lines[1:]))


def test_six_percent_drift_over_three_minutes_uses_constant_sections():
    lines,cues=fixture(31,step=6000,speed=.94)
    t=build_asr_anchored_timeline(lines,cues,source_duration_ms=250000)
    assert len(t.sections)>1
    assert t.anchor_count==len(lines)
    assert all(len(s.anchors)>=3 and s.max_residual_ms<=1500 for s in t.sections)
    assert all(p.live_start_ms==p.line.time_ms+p.shift_ms for p in t.lines)


def test_chorus_sung_twice_more_is_placed_in_live_order():
    lines,_=fixture()
    order=list(range(8))+list(range(4,8))*2+list(range(8,12))
    cues=[SourceCue(str(i),50000+i*6000,53000+i*6000,lines[j].text) for i,j in enumerate(order)]
    t=build_asr_anchored_timeline(lines,cues,source_duration_ms=200000)
    assert [p.position for p in t.lines]==order
    assert [p.live_start_ms for p in t.lines]==[c.source_start_ms for c in cues]
    assert t.anchor_count==20 and t.coverage==1


def test_sparse_anchors_are_pending_not_rejected():
    lines,cues=fixture()
    with pytest.raises(AsrTimelinePending,match='anchors cover'):
        build_asr_anchored_timeline(lines,[cues[i] for i in [0,2,4,6,8,11]],source_duration_ms=200000)


def test_missing_head_is_pending():
    lines,cues=fixture()
    with pytest.raises(AsrTimelinePending,match='head'):
        build_asr_anchored_timeline(lines,cues[2:],source_duration_ms=200000)


def test_split_asr_cues_keep_source_onset():
    lines,cues=fixture()
    split=[]
    for c in cues:
        first,last=c.text.split(' ',1)
        split += [replace(c,cue_id=c.cue_id+'a',source_end_ms=c.source_start_ms+1500,text=first),
                  replace(c,cue_id=c.cue_id+'b',source_start_ms=c.source_start_ms+1500,text=last)]
    t=build_asr_anchored_timeline(lines,split,source_duration_ms=200000)
    assert t.anchor_count==len(lines)
    assert t.offset_ms==40000


def make_run(tmp_path):
    lines,cues=fixture()
    lrc=LrcResult('fixture','Synthetic',None,'fixture://lyrics',tuple(lines))
    run=_write_fake_audio_alignment_run(tmp_path,lrc,candidate_id='synthetic',source_duration_ms=200000,offset_ms=40000)
    return lrc,cues,run


def select(lrc,cues,run):
    return _validated_audio_lrc_selection(run=run,lrc=lrc,candidate_id='synthetic',
        source_media_path=Path(run.source_path),source_duration_ms=200000,min_matched_ratio=.6,asr_anchor_cues=cues)


def test_agy_row_clock_can_jump_thirty_seconds_and_reset(tmp_path):
    lrc,cues,run=make_run(tmp_path)
    payload=json.loads(json.dumps(run.payload))
    for i,row in enumerate(payload['observations']):
        shift=30000 if i%2 else -30000
        row['live_start_ms']+=shift;row['live_end_ms']+=shift
    run=_rebind_fake_audio_alignment_run(run,payload)
    selected=select(lrc,cues,run)
    assert selected[8]==40000
    assert [r['live_time_ms'] for r in selected[2]]==[c.source_start_ms for c in cues]


def test_playback_remains_a_content_rejection(tmp_path):
    lrc,cues,run=make_run(tmp_path)
    payload=json.loads(json.dumps(run.payload))
    payload['live_performance']['mode']='ORIGINAL_OR_BACKGROUND_PLAYBACK'
    run=_rebind_fake_audio_alignment_run(run,payload)
    with pytest.raises(LivePerformanceRejected) as exc:
        select(lrc,cues,run)
    from src.autoslice.song_common import live_performance_failure_reason_codes
    assert live_performance_failure_reason_codes(exc.value.performance)==('SONG_BACKGROUND_PLAYBACK_ONLY','SONG_NOT_LIDOUSHA_SINGING')


def test_no_asr_never_falls_back_to_agy(tmp_path):
    lrc,_,run=make_run(tmp_path)
    with pytest.raises(AsrTimelinePending,match='BCUT ASR'):
        select(lrc,[],run)


def test_sparse_pipeline_returns_unresolved_reason_not_terminal_rejection(tmp_path):
    lrc,cues,run=make_run(tmp_path)
    result=attempt_song_repair(candidate_id='synthetic',cues=cues,anchor_start_ms=50000,
        anchor_end_ms=119000,source_duration_ms=200000,output_dir=tmp_path/'repair',
        pinned_lrc_results=[lrc],source_media_path=Path(run.source_path),
        audio_lrc_aligner=lambda *a:run,asr_anchor_cues=[cues[i] for i in [0,2,4,6,8,11]])
    assert result.repaired is False
    assert result.reason_codes==('SONG_AUDIO_LRC_PROOF_UNRESOLVED',)
    assert not any('rejected' in attempt.detail.lower() for attempt in result.attempts)


@pytest.mark.parametrize("mode", ["drift", "repeats", "tail_repeats", "skew"])
def test_report_replay_and_render_use_each_section_time(tmp_path, mode):
    lines,cues=fixture(31,step=6000,speed=.94)
    if mode=="skew":
        lines,cues=fixture()
        cues[-1]=replace(cues[-1],source_start_ms=cues[-1].source_start_ms+2000,
                         source_end_ms=cues[-1].source_end_ms+2000)
    if mode in {"repeats", "tail_repeats"}:
        lines,_=fixture()
        order=(list(range(12))+list(range(4,8))*2 if mode=="tail_repeats"
               else list(range(8))+list(range(4,8))*2+list(range(8,12)))
        cues=[SourceCue(str(i),50000+i*6000,53000+i*6000,lines[j].text) for i,j in enumerate(order)]
    lrc=LrcResult('fixture','Synthetic',None,'fixture://lyrics',tuple(lines))
    run=_write_fake_audio_alignment_run(tmp_path,lrc,candidate_id='synthetic',source_duration_ms=260000,offset_ms=40000)
    if mode in {"repeats", "tail_repeats"}:
        payload=json.loads(json.dumps(run.payload))
        payload["live_arrangement"]["post_song_transition_ms"]=170000
        payload["post_song_talk_start_ms"]=170000
        run=_rebind_fake_audio_alignment_run(run,payload)
    talk_ms=run.payload["post_song_talk_start_ms"]
    cues.append(SourceCue("talk",talk_ms,talk_ms+3000,"spoken transition"))
    # End transition is independently observed and remains later than the actual tail.
    selected=_validated_audio_lrc_selection(run=run,lrc=lrc,candidate_id='synthetic',source_media_path=Path(run.source_path),
        source_duration_ms=260000,min_matched_ratio=.6,asr_anchor_cues=cues)
    out=tmp_path/'repair';out.mkdir()
    result=_materialize_song_repair(candidate_id='synthetic',output_dir=out,provider_name='fixture',
        selected=selected,audio_alignment_run=run,audio_lrc_variant_attempts=[],attempts=[])
    report_path=Path(result.lyrics_alignment['alignment_report_path'])
    report=json.loads(report_path.read_text())
    if mode=="skew":
        assert report['anchor_line_count']==12 and report['dropped_anchor_count']==0
        assert report['offset_ms']==40500 and len(report['timing_sections'])==1
    assert verify_timing_report(report=report,raw_rows=run.payload['observations'],expected_raw_sha256=run.output_sha256) is None
    from src.autoslice.live_source_review import _verify_live_performance_observation
    assert _verify_live_performance_observation(result.lyrics_alignment, output_dir=out) is None
    from src.autoslice.song_completion import _validate_alignment_report, _is_int
    failures=[]
    _validate_alignment_report(report=report, alignment=dict(result.lyrics_alignment),
        boundary=dict(result.song_boundary), boundary_zero=selected[8],
        record={"candidate_id":"synthetic"},failures=failures,is_int=_is_int)
    assert failures==[]
    timeline,offset=_load_lyric_timeline({"lyrics_alignment": result.lyrics_alignment}, output_dir=out)
    srt=tmp_path/'output.srt'
    _write_lyric_timeline_srt(timeline,offset,selected[6],selected[7],srt)
    from src.autoslice.song_alignment import _parse_asr_anchor_srt
    rendered=_parse_asr_anchor_srt(srt)
    assert [c.source_start_ms+selected[6] for c in rendered]==[r['live_time_ms'] for r in selected[2]]
    if mode=="skew":
        assert all(abs(c.source_start_ms+selected[6]-a.source_start_ms)<=1500
                   for c,a in zip(rendered,cues))
    # Rebinding the report hash cannot make an unverified timing mutation valid.
    for mutation in ["unknown_model", "missing_model", "missing_lyric_live", "missing_alignment_live", "drift"]:
        changed=json.loads(json.dumps(report))
        if mutation=="unknown_model":
            changed['timing_model']='unsupported.v99'
        elif mutation=="missing_model":
            del changed['timing_model']
        elif mutation=="missing_lyric_live":
            del changed['lyric_lines'][0]['live_time_ms']
        elif mutation=="missing_alignment_live":
            del changed['alignment'][0]['live_time_ms']
        else:
            changed['lyric_lines'][0]['live_time_ms']+=2000
            changed['alignment'][0]['live_time_ms']+=2000
        report_path.write_text(json.dumps(changed))
        alignment=dict(result.lyrics_alignment,
            alignment_report_sha256=hashlib.sha256(report_path.read_bytes()).hexdigest())
        assert _load_lyric_timeline({'lyrics_alignment':alignment},output_dir=out) is None
        assert _verify_live_performance_observation(alignment,output_dir=out) is not None
        failures=[]
        _validate_alignment_report(report=changed,alignment=alignment,boundary=dict(result.song_boundary),
            boundary_zero=selected[8],record={'candidate_id':'synthetic'},failures=failures,is_int=_is_int)
        assert failures
    report_path.write_text(json.dumps(report))
    cue_path=Path(report['asr_timing_input']['path']);cue_path.write_text(cue_path.read_text()+' ')
    assert verify_timing_report(report=report,raw_rows=run.payload['observations'],expected_raw_sha256=run.output_sha256)=='ASR cue file hash mismatch'
    alignment=dict(result.lyrics_alignment,
        alignment_report_sha256=hashlib.sha256(report_path.read_bytes()).hexdigest())
    assert _load_lyric_timeline({'lyrics_alignment':alignment},output_dir=out) is None


def test_legacy_report_ignores_unverified_live_time_when_rendering(tmp_path):
    lrc,cues,run=make_run(tmp_path)
    payload=json.loads(json.dumps(run.payload))
    for evidence,index in zip(payload['live_performance']['evidence'],[0,6,11]):
        evidence['time_ms']=payload['observations'][index]['live_start_ms']
    run=_rebind_fake_audio_alignment_run(run,payload)
    from src.autoslice.song_performance import _build_audio_observation_alignment
    observation=_build_audio_observation_alignment(run=run,lrc=lrc,candidate_id='synthetic',
        effective_duration_ms=200000,asr_anchor_cues=cues)
    legacy=list(select(lrc,cues,run))
    legacy[2]=observation.agy_alignment
    legacy[3]=observation.agy_alignment
    legacy[10]={'offset_basis':'agy_median','agy_offset_ms':40000}
    out=tmp_path/'legacy';out.mkdir()
    result=_materialize_song_repair(candidate_id='synthetic',output_dir=out,provider_name='fixture',
        selected=tuple(legacy),audio_alignment_run=run,audio_lrc_variant_attempts=[],attempts=[])
    assert result.repaired is True
    report_path=Path(result.lyrics_alignment['alignment_report_path'])
    report=json.loads(report_path.read_text())
    assert 'timing_model' not in report
    expected=[(line.time_ms,line.text) for line in lrc.lines]
    assert _load_lyric_timeline({'lyrics_alignment':result.lyrics_alignment},output_dir=out)==(expected,40000)
    for key in ['lyric_lines','alignment']:
        for row in report[key]:
            row['live_time_ms']=row['lrc_time_ms']+70000
    report_path.write_text(json.dumps(report))
    alignment=dict(result.lyrics_alignment,
        alignment_report_sha256=hashlib.sha256(report_path.read_bytes()).hexdigest())
    from src.autoslice.song_completion import _validate_alignment_report, _is_int
    failures=[]
    _validate_alignment_report(report=report,alignment=alignment,boundary=dict(result.song_boundary),
        boundary_zero=40000,record={'candidate_id':'synthetic'},failures=failures,is_int=_is_int)
    assert failures==[],failures
    from src.autoslice.live_source_review import _verify_live_performance_observation
    assert _verify_live_performance_observation(alignment,output_dir=out) is None
    loaded=_load_lyric_timeline({'lyrics_alignment':alignment},output_dir=out)
    assert loaded==(expected,40000)
    srt=tmp_path/'legacy.srt'
    _write_lyric_timeline_srt(*loaded,0,200000,srt)
    from src.autoslice.song_alignment import _parse_asr_anchor_srt
    assert [c.source_start_ms for c in _parse_asr_anchor_srt(srt)]==[line.time_ms+40000 for line in lrc.lines]


def test_production_notice_is_metadata_not_a_sung_line():
    from src.autoslice.song_common import is_lrc_non_lyric_metadata
    assert is_lrc_non_lyric_metadata("【此版本为正式授权翻唱作品】")
    assert not is_lrc_non_lyric_metadata("amber lantern")


def test_cached_reference_with_notice_keeps_original_input_binding(tmp_path):
    lines, cues = fixture()
    canonical = LrcResult('fixture', 'Synthetic', None, 'fixture://lyrics',
                          (LrcLine(5000, '【此版本为正式授权翻唱作品】'), *lines))
    run = _write_fake_audio_alignment_run(tmp_path, canonical, candidate_id='synthetic',
                                          source_duration_ms=200000, offset_ms=40000)
    clean = replace(canonical, lines=tuple(lines))
    manifest_bytes = Path(run.manifest_path).read_bytes()
    result = attempt_song_repair(candidate_id='synthetic', cues=cues, anchor_start_ms=50000,
        anchor_end_ms=119000, source_duration_ms=200000, output_dir=tmp_path/'repair',
        pinned_lrc_results=[clean], source_media_path=Path(run.source_path),
        audio_lrc_aligner=lambda *args: pytest.fail('bound observation must not be redispatched'),
        prior_audio_lrc_runs=[run], asr_anchor_cues=cues)
    assert result.repaired is True
    report = json.loads(Path(result.lyrics_alignment['alignment_report_path']).read_text())
    assert [r['text'] for r in report['lyric_lines']] == [l.text for l in lines]
    assert [r['lrc_index'] for r in report['lyric_lines']] == list(range(1,13))
    assert report['canonical_line_count'] == 13
    assert Path(run.manifest_path).read_bytes() == manifest_bytes
    assert verify_timing_report(report=report, raw_rows=run.payload['observations'],
                                expected_raw_sha256=run.output_sha256) is None
    from src.autoslice.live_source_review import _verify_live_performance_observation
    assert _verify_live_performance_observation(result.lyrics_alignment, output_dir=tmp_path/'repair') is None
