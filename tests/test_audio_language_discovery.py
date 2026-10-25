import json

import pytest

from src.autoslice import audio_language_discovery as discovery
from src.autoslice import foreign_span_witness as foreign


@pytest.fixture(autouse=True)
def source_media(tmp_path):
    for name in ("audio.mp3", "body.mp3"):
        (tmp_path / name).write_bytes(b"actual-fixture-source")


def srt(*texts):
    return "\n\n".join(f"{i+1}\n00:00:{i*2:02d},000 --> 00:00:{i*2+1:02d},000\n{text}"
                       for i, text in enumerate(texts)) + "\n"


def scan(*, spans=(), anomalies=(), turns=()):
    return {"status": "OBSERVED", "foreign_spans": list(spans),
            "semantic_anomalies": list(anomalies), "speech_turns": list(turns)}


def run(tmp_path, monkeypatch, current, detection, transcripts=None, judge=None, native=None, confirmed=None, initial_authority=None):
    calls = {"scan": 0, "heard": [], "judge": 0}
    def scanner(**kwargs):
        calls["scan"] += 1
        assert kwargs["srt_text"] == current
        return detection
    def witness(**kwargs):
        assert kwargs["native_provider"] == native
        calls["heard"].extend(r["cue_index"] for r in kwargs["rows"])
        result = []
        for row in kwargs["rows"]:
            text, language = (transcripts or {}).get(row["cue_index"], (row["text"], "zh"))
            result.append({**row, "exact_transcript": text, "audible_language": language,
                           "audio_sha256": "a"*64, "provider": native or "agy", "witnessed": False})
        return result
    def cpa(prompt):
        calls["judge"] += 1
        return (judge or (lambda p: json.dumps({"choice": "PROPOSED", "candidate_id": "PROPOSAL",
                                               "reason": "The native script matches the actual observed speech."})))(prompt)
    monkeypatch.setattr(foreign, "_witness_rows", witness)
    authority = initial_authority if initial_authority is not None else {}
    result, audit = discovery.repair_audio_language_candidates(
        media_path=tmp_path/'audio.mp3', srt_text=current, out_root=tmp_path,
        cid='fixture', llm_call=cpa, authority=authority, scan_call=scanner,
        native_provider=native, confirmed_audio_rows=confirmed)
    return result, audit, authority, calls


def test_audio_confirmed_foreign_expands_to_fluent_chinese_neighbors(tmp_path, monkeypatch):
    current = srt("今天很好", "明天也好", "大丈夫", "你说得对", "这很正常", "大家一起回去")
    observed = scan(spans=[{"start_ms": 4000, "end_ms": 5000, "language": "ja"}],
                    turns=[{"start_ms": 0, "end_ms": 11000}])
    result, audit, _, calls = run(tmp_path, monkeypatch, current, observed)
    assert calls == {"scan": 1, "heard": [3, 2, 1, 4, 5], "judge": 0}
    assert result == current
    assert audit['status'] == 'RESOLVED'


def test_whole_audio_scan_routes_fluent_hanzi_to_existing_cpa_owner(tmp_path, monkeypatch):
    current = srt("今天下午去图书馆")
    observed = scan(spans=[{"start_ms": 100, "end_ms": 900, "language": "ja"}])
    result, audit, authority, calls = run(tmp_path, monkeypatch, current, observed,
                                        {1: ("今日は図書館に行く", "ja")})
    assert result == srt("今日は図書館に行く")
    assert calls == {"scan": 1, "heard": [1], "judge": 1}
    assert authority['entity_repairs'][0]['mode'] == 'final_foreign_script_cpa_adjudication'
    assert audit['applied_count'] == 1


def test_incoherent_hanzi_without_foreign_scan_hit_is_heard_and_expands(tmp_path, monkeypatch):
    current = srt("普通上句", "夏天桌子飞七楼", "普通下句")
    observed = scan(anomalies=[{"cue_index": 2, "reason": "unexplained incoherent object/predicate"}],
                    turns=[{"start_ms": 0, "end_ms": 5000}])
    result, _, _, calls = run(tmp_path, monkeypatch, current, observed,
                              {2: ("夏休みが終わった", "ja")})
    assert '夏休みが終わった' in result
    assert calls['heard'] == [2, 1, 3]


def test_pure_chinese_has_one_scan_and_no_exact_or_cpa_calls(tmp_path, monkeypatch):
    current = srt("今天很好", "我们回家吧")
    result, audit, authority, calls = run(tmp_path, monkeypatch, current, scan())
    assert result == current and authority == {}
    assert audit['status'] == 'CLEAN'
    assert calls == {"scan": 1, "heard": [], "judge": 0}


def test_uncertain_candidate_does_not_block_or_lose_resolved_other_cue(tmp_path, monkeypatch):
    current = srt("多谢你", "七船飞机")
    observed = scan(spans=[{"start_ms": 0, "end_ms": 3000, "language": "mixed"}])
    result, audit, _, calls = run(tmp_path, monkeypatch, current, observed,
                                 {1: ("ありがとう", "ja"), 2: ("", "none")})
    assert result == srt("ありがとう", "七船飞机")
    assert audit['status'] == 'PENDING_CURRENT_RISK'
    assert audit['disclosures'][0]['cue_index'] == 2
    assert calls['judge'] == 1


def test_neighbor_boundary_gap_and_speaker_turn_are_respected():
    current = srt("一", "二", "三", "四", "五")
    rows = discovery.adjacent_candidates(current, {3}, [{"start_ms": 2000, "end_ms": 7000}], {3})
    assert {r['cue_index'] for r in rows} == {2, 4}
    current = current.replace('00:00:06,000', '00:00:06,501').replace('00:00:07,000', '00:00:07,501')
    rows = discovery.adjacent_candidates(current, {3}, [{"start_ms": 0, "end_ms": 9000}], {3})
    assert {r['cue_index'] for r in rows} == {1, 2}


def test_native_witness_selection_keeps_original_language_and_cpa(tmp_path, monkeypatch):
    current = srt("我们来试看")
    result, _, _, calls = run(tmp_path, monkeypatch, current,
                              scan(spans=[{"start_ms": 0, "end_ms": 1000, "language": "mixed"}]),
                              {1: ("我们try一下", None)}, native="moss")
    assert result == srt("我们try一下") and calls['judge'] == 1


@pytest.mark.parametrize('bad', [{}, {'foreign_spans': [], 'semantic_anomalies': [], 'speech_turns': None},
    {'foreign_spans': [{'start_ms': 0, 'end_ms': 1000, 'language': 'zh'}], 'semantic_anomalies': [], 'speech_turns': []},
    {'foreign_spans': [], 'semantic_anomalies': [{'cue_index': True, 'reason': 'bad'}], 'speech_turns': []}])
def test_invalid_scan_cannot_be_clean(bad):
    with pytest.raises(ValueError):
        discovery._parse_scan(json.dumps(bad))


def test_provider_failure_discloses_without_changing_subtitle(tmp_path):
    def fail(**kwargs):
        raise RuntimeError('transport timeout')
    current = srt('正常中文')
    result, audit = discovery.repair_audio_language_candidates(
        media_path=tmp_path/'audio.mp3', srt_text=current, out_root=tmp_path, cid='fixture',
        authority={}, llm_call=None, scan_call=fail)
    assert result == current
    assert audit['status'] == 'OBSERVATION_UNAVAILABLE'
    assert audit['disclosures'][0]['reason'] == 'AUDIO_LANGUAGE_DISCOVERY_UNAVAILABLE'


def test_normal_producer_finalization_runs_audio_discovery(tmp_path, monkeypatch):
    from src.autoslice import producer_text_pipeline as pipeline
    current = srt('这是上文', '我们明天再来', '这是下文')
    calls = []
    def scanner(**kwargs):
        calls.append(kwargs)
        return scan(spans=[{"start_ms": 2000, "end_ms": 3000, "language": "ja"}])
    def witness(**kwargs):
        return [{**kwargs['rows'][0], 'exact_transcript': 'また明日来ます',
                 'audible_language': 'ja', 'audio_sha256': 'b'*64, 'provider': 'agy'}]
    monkeypatch.setattr(discovery, 'scan_audio_languages', scanner)
    monkeypatch.setattr(foreign, '_witness_rows', witness)
    monkeypatch.setattr(pipeline, '_build_final_review_llm_call', lambda: lambda prompt: json.dumps(
        {'choice': 'PROPOSED', 'candidate_id': 'PROPOSAL', 'reason': 'The original language is audible.'}))
    authority = {'status': 'PASS', 'source_truth_preview_receipts': {
        stage: {'schema_version': 'source-truth-deterministic-preview.v1', 'ledger_sha256': None}
        for stage in ('pre_entity_arbitration', 'pre_correction_review')}}
    result = pipeline._finalize_text_evidence(
        spec={'pieces': []}, durations=[], srt_text=current, chat_authority_audit=authority,
        transcript_entity_audit={'status': 'PASS'}, referent_groups=[], final_review_audit={},
        song_name_candidates=[], known_songs_path=tmp_path/'songs.json', session_topic_authorities=(),
        source_language_witness_srt=current, text_override_path=None, source_truth_ledger_path=None,
        out_root=tmp_path, cid='fixture', padded=tmp_path/'body.mp3')
    assert result.srt_text == srt('这是上文', 'また明日来ます', '这是下文')
    assert len(calls) == 1 and calls[0]['media_path'] == tmp_path/'body.mp3'
    assert authority['audio_language_discovery_audit']['applied_count'] == 1
    assert authority['entity_repairs'][-1]['after'] == ['また明日来ます']


def test_scan_parser_is_applied_before_success_cache(tmp_path, monkeypatch):
    import subprocess
    from pathlib import Path
    monkeypatch.setattr(discovery.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 0, '1.0', ''))
    monkeypatch.setattr(foreign, '_extract_span_audio', lambda media, start, end, output: output.write_bytes(b'audio'))
    seen = []
    def observe(**kwargs):
        seen.append(kwargs['algorithm_id'])
        raw = json.dumps(scan(spans=[{'start_ms': 0, 'end_ms': 2000, 'language': 'ja'}]))
        return kwargs['parse_observation'](raw), 'agy', None, {}
    monkeypatch.setattr(foreign, '_observe_audio', observe)
    with pytest.raises(ValueError, match='OUTSIDE_AUDIO'):
        discovery.scan_audio_languages(media_path=Path('media'), srt_text=srt('中文'), out_root=tmp_path, cid='fixture')
    assert seen == [discovery._SCAN_ALGORITHM]


def test_other_languages_do_not_compare_equal_after_script_stripping(tmp_path, monkeypatch):
    current = srt('Он')
    result, _, _, calls = run(tmp_path, monkeypatch, current,
                              scan(spans=[{'start_ms': 0, 'end_ms': 1000, 'language': 'other'}]),
                              {1: ('Она', 'other')})
    assert result == srt('Она') and calls['judge'] == 1


def test_scan_success_cache_avoids_repeated_audio_calls(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    monkeypatch.setattr(discovery.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 0, '1.0', ''))
    monkeypatch.setattr(foreign, '_extract_span_audio', lambda media, start, end, output: output.write_bytes(b'audio'))
    calls = []
    def agy(**kwargs):
        calls.append(kwargs['prompt'])
        return kwargs['parse_observation'](json.dumps(scan()))
    monkeypatch.setattr(foreign, '_observe_with_agy', agy)
    kwargs = dict(media_path=tmp_path/'audio.mp3', srt_text=srt('今天好'), out_root=tmp_path, cid='fixture')
    first = discovery.scan_audio_languages(**kwargs)
    second = discovery.scan_audio_languages(**kwargs)
    assert len(calls) == 1
    assert not first['served_from_cache'] and second['served_from_cache']


def test_prior_audio_confirmed_foreign_expands_without_rehearing_seed(tmp_path, monkeypatch):
    current = srt('中文一', '中文二', '大丈夫', '中文四', '中文五')
    prior = {'cue_index': 3, 'start_ms': 4000, 'end_ms': 5000,
             'audible_language': 'ja', 'exact_transcript': '大丈夫', 'audio_sha256': 'c'*64}
    result, audit, _, calls = run(tmp_path, monkeypatch, current,
                                 scan(turns=[{'start_ms': 0, 'end_ms': 9000}]), confirmed=[prior])
    assert result == current
    assert calls['heard'] == [2, 1, 4, 5]
    assert audit['prior_confirmed_cue_indexes'] == [3]


def test_newly_heard_foreign_neighbor_extends_but_chinese_stops(tmp_path, monkeypatch):
    current = srt('中文一', '中文二', '大丈夫', '中文四', '再见了', '中文六', '中文七', '中文八')
    _, audit, _, calls = run(tmp_path, monkeypatch, current,
                            scan(spans=[{'start_ms': 4000, 'end_ms': 5000, 'language': 'ja'}],
                                 turns=[{'start_ms': 0, 'end_ms': 15000}]),
                            {5: ('さようなら', 'ja')})
    assert set(calls['heard']) == {1, 2, 3, 4, 5, 6, 7}
    assert len(calls['heard']) == 7
    assert 8 not in {r['cue_index'] for r in audit['candidates']}


def test_owner_registration_preserves_live_references_to_other_audits(tmp_path, monkeypatch):
    source_audit = {"status": "DEFERRED_TO_SOURCE_SUBTITLE_TRUTH"}
    authority = {"final_source_language_preservation_audit": source_audit}
    _, _, result, _ = run(
        tmp_path, monkeypatch, srt("中文误识"),
        scan(spans=[{"start_ms": 0, "end_ms": 1000, "language": "ja"}]),
        {1: ("日本語です", "ja")}, initial_authority=authority)
    assert result["final_source_language_preservation_audit"] is source_audit
    source_audit["status"] = "RESOLVED_BY_SOURCE_SUBTITLE_TRUTH"
    assert result["final_source_language_preservation_audit"]["status"] == "RESOLVED_BY_SOURCE_SUBTITLE_TRUTH"
    assert result["entity_repairs"][0]["after"] == ["日本語です"]
