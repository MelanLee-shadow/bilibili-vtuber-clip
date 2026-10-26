import copy
import json
import hashlib

import pytest

from src.autoslice.editorial_subtitle import validate_authority, validate_repair, gift_nominations
from src.autoslice.final_review_auditor import audit_final_subtitles, adjudicate_exact_release_findings, adjudicate_context_finding
from src.autoslice.producer_package_finalization import _apply_exact_final_cpa_repairs, _register_exact_final_cpa_repairs
from src.autoslice.producer_text_pipeline import _continuation_validate_exact_final_owner
from src.autoslice.final_review_contract import _validate_exact_final_cpa_self_heal

TEXT = '谢谢有人看到你的人鱼'
SRT = '1\n00:00:01,000 --> 00:00:02,000\n' + TEXT + '\n'
CONTEXT = {'pieces': [{'source_media_sha256': 'a' * 64}], 'structured_chat': [
    {'kind': 'gift', 'offset_ms': 800, 'text': '小花花', 'sender': '小***', 'source_event_id': 'e1', 'source_sha256': 'b' * 64},
    {'kind': 'gift', 'offset_ms': 500, 'text': '流星雨', 'sender': '有***', 'source_event_id': 'e2', 'source_sha256': 'b' * 64},
]}


def h(text):
    return 'sha256:' + hashlib.sha256(text.encode()).hexdigest()


def witness(request):
    return {'schema_version': 'subtitle-span-acoustic-witness.v1', 'request_sha256': request['request_sha256'],
            'status': 'OBSERVED', 'target_audible': True, 'heard_pinyin': 'xie xie you ren kan dao ni de ren yu',
            'source_media_sha256': 'a' * 64, 'audio_clip_sha256': 'c' * 64}


def answer(**changes):
    value = {'status': 'RESOLVED', 'mode': 'bound_event_spelling', 'edit_start': len(TEXT)-2, 'edit_end': len(TEXT),
             'replacement': '流星雨', 'reliable_fragments': [{'text': TEXT[:-2], 'basis': '感谢和称呼清楚'}],
             'evidence_refs': ['audio', 'event:1'], 'meaning_basis': '谢礼语法与有字称呼对应e2', 'no_new_facts': True,
             'event_index': 1, 'event_field': 'text', 'referent_basis': '称呼有字与e2发送者对应',
             'alternatives_considered': '更近e1发送者不对应这次称呼', 'literal_uncertainty': '礼物名快语音不清'}
    value.update(changes)
    return value


def run(tmp_path, monkeypatch, *, response=None, context=CONTEXT, text=SRT, screen=None, offset=5000):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    discovery = lambda _: json.dumps({'findings': [{'cue': 1, 'kind': 'entity', 'repair_class': 'editorial_rendering',
                                                   'proposed_full_cue': None, 'why': '核对谢礼指代',
                                                   'content_risk': 'factual_error',
                                                   'risk_basis': '本合成用例的原句指代或发声类型待核对；实际编辑仍须后续来源和原答授权。'}]})
    findings = audit_final_subtitles(text, llm_call=discovery, extract_json=json.loads, candidate_context=context,
                                    source_media_timeline_offset_ms=offset)
    calls = []
    def judge(prompt):
        calls.append(prompt)
        return json.dumps(response or answer(), ensure_ascii=False)
    judge.cpa_cache_identity = {'transport': 'test', 'models': ['test'], 'effort': 'high'}
    unresolved, resolved = adjudicate_exact_release_findings(text, findings, entity_verifier=witness,
                                                            judge_llm_call=judge, clip_context=context,
                                                            source_media_timeline_offset_ms=offset, screen_read_probe=screen)
    assert not resolved  # Proposed edits require actual self-heal plus a new final review.
    output, repairs = _apply_exact_final_cpa_repairs(text, {'findings': unresolved}, clip_context=context)
    return output, repairs, unresolved, calls


def test_ordinary_discovery_to_edit_registration_json_restart_and_pending_review(tmp_path, monkeypatch):
    output, repairs, rows, calls = run(tmp_path, monkeypatch)
    assert len(calls) == 1 and len(repairs) == 1
    assert TEXT[:-2] + '流星雨' in output and '小花花' not in output
    repair = repairs[0]
    assert repair['literal_transcription'] is False
    assert 'acoustic_witness_binding' not in repair  # No fabricated literal-fit evidence.
    authority = {'exact_final_cpa_self_heal': {
        'schema_version': 'exact-final-cpa-self-heal-audit.v1', 'status': 'REVIEW_PENDING',
        'passes': [{'schema_version': 'exact-final-cpa-self-heal-pass.v1', 'pass_index': 1,
                    'input_srt_sha256': h(SRT), 'output_srt_sha256': h(output), 'repairs': repairs}],
    }}
    _register_exact_final_cpa_repairs(authority, repairs, delivery_start_ms=5000)
    saved = json.loads(json.dumps(authority))
    _validate_exact_final_cpa_self_heal(saved, expected_srt_sha256=h(output), allow_pending=True)
    _continuation_validate_exact_final_owner(saved['entity_repairs'][0], row_index=0, chat_authority=saved)
    with pytest.raises(ValueError):
        _validate_exact_final_cpa_self_heal(saved, expected_srt_sha256=h(output))
    for change in ('after', 'raw', 'offset'):
        corrupt = copy.deepcopy(repair)
        if change == 'after':
            corrupt['after'] = '谢谢新身份的流星雨'
        elif change == 'raw':
            corrupt['editorial_authority']['raw_completion'] += 'tamper'
        else:
            corrupt['editorial_authority']['request']['source_media_timeline_offset_ms'] = 6000
        with pytest.raises((ValueError, KeyError)):
            validate_repair(corrupt)


def test_nearest_event_not_a_referent_and_source_or_reliable_fragment_drift_blocks(tmp_path, monkeypatch):
    for i, bad in enumerate((answer(event_index=0), answer(referent_basis=''),
                answer(edit_start=0, reliable_fragments=[{'text': '谢谢', 'basis': 'clear'}]),
                answer(no_new_facts=False))):
        output, repairs, rows, _ = run(tmp_path / str(i), monkeypatch, response=bad)
        assert output == SRT and repairs == []
    _, repairs, _, _ = run(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match='CONTEXT_DRIFT'):
        validate_authority(repairs[0]['editorial_authority'], clip_context={})


def test_missing_event_can_summarize_only_confirmed_thanks_after_screen_attempt(tmp_path, monkeypatch):
    generic = answer(mode='gift_thanks_summary', edit_start=0, replacement='谢谢礼物', reliable_fragments=[],
                     evidence_refs=['audio'], gift_thanks_confirmed=True, name_sources_exhausted=True)
    calls = []
    def no_screen_text(start, end):
        calls.append((start, end))
        return {'status': 'UNAVAILABLE'}
    output, repairs, _, _ = run(tmp_path / 'screen-unavailable', monkeypatch, response=generic, context={}, screen=no_screen_text)
    assert calls == [(6000, 7000)]
    assert '谢谢礼物' in output and repairs[0]['rendering_mode'] == 'gift_thanks_summary'
    _, absent_screen_repairs, _, _ = run(tmp_path / 'screen-absent', monkeypatch, response=generic, context={})
    assert absent_screen_repairs[0]['editorial_authority']['request']['screen_attempted'] is False
    assert absent_screen_repairs[0]['editorial_authority']['request']['screen_availability'] == 'NO_SCREEN_PROVIDER'
    _, denied, _, _ = run(tmp_path / 'denied', monkeypatch, response={**generic, 'gift_thanks_confirmed': False}, context={}, screen=no_screen_text)
    assert denied == []


@pytest.mark.parametrize('text', ['谢谢你的礼物', '谢谢大家的礼物', '感谢您送来的礼物'])
def test_generic_thanks_has_no_unbound_name_slot(text):
    text_srt = '1\n00:00:00,000 --> 00:00:02,000\n' + text + '\n'
    assert gift_nominations(text_srt, {}) == []
    for specific in ['谢谢小明的礼物', '谢谢你的草莓']:
        specific_srt = text_srt.replace(text, specific)
        assert len(gift_nominations(specific_srt, {})) == 1


def test_generic_gift_detector_retires_its_old_nomination_without_hiding_model_risk():
    from src.autoslice.editorial_subtitle import GIFT_NOMINATION_REASON, withdrawn_generic_gift_nomination
    from src.autoslice.final_review_auditor import _normalize_discovery_materials
    from src.autoslice.jingting_chunker import parse_srt_cues
    text = '1\n00:00:00,000 --> 00:00:02,000\n谢谢你的礼物\n'
    old = {'cue': 1, 'kind': 'entity', 'repair_class': 'editorial_rendering',
           'suspect': '谢谢你的礼物', 'why': GIFT_NOMINATION_REASON, 'proposed_full_cue': None}
    assert withdrawn_generic_gift_nomination(old, text)
    assert _normalize_discovery_materials([], [old], parse_srt_cues(text), current_srt_text=text) == []
    critical = {**old, 'content_risk': 'factual_error', 'risk_basis': 'Specific identity evidence contradicts this statement.'}
    assert not withdrawn_generic_gift_nomination(critical, text)
    assert _normalize_discovery_materials([critical], [old], parse_srt_cues(text), current_srt_text=text) == [critical]


def test_early_stage_defers_without_false_mutation_and_unbound_events_never_nominate():
    findings = gift_nominations(SRT, CONTEXT)
    assert len(findings) == 1
    assert len(gift_nominations(SRT, {'structured_chat': [{**CONTEXT['structured_chat'][0], 'source_sha256': ''}]})) == 1
    output, audit = adjudicate_context_finding(SRT, {'repair_class': 'editorial_rendering'}, entity_verifier=None)
    assert output == SRT and audit['repaired'] is False
    assert audit['reason_code'] == 'EDITORIAL_EXACT_FINAL_REVIEW_REQUIRED'


def test_gift_discovery_uses_source_clock_without_accepting_an_early_unrelated_event():
    text = SRT.replace(TEXT, '谢谢小明的流星雨')
    event = {**CONTEXT['structured_chat'][1], 'offset_ms': 241500}
    assert gift_nominations(text, {'structured_chat': [event]},
                            source_media_timeline_offset_ms=240000) == []
    assert len(gift_nominations(text, {'structured_chat': [event]})) == 1
    early = {**event, 'offset_ms': 1500}
    assert len(gift_nominations(text, {'structured_chat': [early]},
                                source_media_timeline_offset_ms=240000)) == 1
    assert audit_final_subtitles(text, llm_call=lambda _: '{"findings":[]}',
        extract_json=json.loads, candidate_context={'structured_chat': [event]},
        source_media_timeline_offset_ms=240000) == []


def test_editorial_event_clock_survives_restart_and_rejects_legacy_wrong_pool(tmp_path, monkeypatch):
    from src.autoslice.editorial_subtitle import _events, digest, sha, PROMPT, OMISSION_PROMPT, REFERENCE_PROMPT
    context = copy.deepcopy(CONTEXT)
    source_rows = [{**row, 'offset_ms': row['offset_ms'] + 240000} for row in context['structured_chat']]
    context['structured_chat'].extend(source_rows)
    _, repairs, _, _ = run(tmp_path, monkeypatch, context=context, offset=240000)
    assert len(repairs) == 1
    receipt = json.loads(json.dumps(repairs[0]['editorial_authority']))
    request = receipt['request']
    assert request['schema_version'] == 'editorial-subtitle-request.v2'
    assert request['source_event_window'] == [241000, 242000]
    assert [row['offset_ms'] for row in request['events']] == [240800, 240500]
    assert validate_authority(receipt, clip_context=context) == TEXT[:-2] + '流星雨'
    before = copy.deepcopy(receipt)
    legacy = copy.deepcopy(receipt)
    old = legacy['request']
    old['schema_version'] = 'editorial-subtitle-request.v1'
    old.pop('source_event_window'); old.pop('events_timebase')
    old['events'] = _events(context, *old['window'])
    legacy['request_sha256'] = digest(old)
    legacy['prompt'] = PROMPT + OMISSION_PROMPT + REFERENCE_PROMPT + json.dumps(old, ensure_ascii=False, sort_keys=True)
    legacy['prompt_sha256'] = sha(legacy['prompt'])
    with pytest.raises(ValueError, match='EVENT_POOL_DRIFT'):
        validate_authority(legacy, clip_context=context)
    assert receipt == before


def test_bound_context_and_event_list_refs_replay_legacy_receipts(tmp_path, monkeypatch):
    from src.autoslice.editorial_subtitle import _decision, PROMPT, OMISSION_PROMPT, sha
    _, repairs, _, _ = run(tmp_path, monkeypatch)
    authority = repairs[0]['editorial_authority']
    request = copy.deepcopy(authority['request'])
    request['context_before'] = request['check_request']['context_before'] = '前面在连续谢礼'
    request['context_after'] = request['check_request']['context_after'] = '后面继续致谢'
    response = answer(evidence_refs=['audio', 'event:1', 'events', 'context_before', 'check_request.context_after'])
    assert _decision(request, response) == TEXT[:-2] + '流星雨'
    for invalid_ref in ('title', 'check_request.unknown', 'event:99', 'screen'):
        with pytest.raises(ValueError, match='EVIDENCE_REF_INVALID'):
            _decision(request, {**response, 'evidence_refs': ['audio', invalid_ref]})
    request['context_before'] = '与原check不同的文本'
    with pytest.raises(ValueError, match='EVIDENCE_REF_INVALID'):
        _decision(request, response)
    # Old exact-bound prompts remain valid after the emitter adds explicit
    # reference instructions; no rewriting of saved requests is required.
    for prefix in (PROMPT, PROMPT + OMISSION_PROMPT):
        old = copy.deepcopy(authority)
        old['request']['schema_version'] = 'editorial-subtitle-request.v1'
        old['request'].pop('events_timebase')
        old['request'].pop('source_event_window')
        from src.autoslice.editorial_subtitle import digest
        old['request_sha256'] = digest(old['request'])
        old['prompt'] = prefix + json.dumps(old['request'], ensure_ascii=False, sort_keys=True)
        old['prompt_sha256'] = sha(old['prompt'])
        assert validate_authority(old, clip_context=CONTEXT) == TEXT[:-2] + '流星雨'


def test_faithful_partial_omission_preserves_reliable_thanks_and_gift(tmp_path, monkeypatch):
    text = '谢谢不明昵称的流星雨'
    srt = SRT.replace(TEXT, text)
    response = answer(mode='faithful_paraphrase', edit_start=2, edit_end=text.index('流星雨'), replacement='',
                      reliable_fragments=[{'text': '谢谢', 'basis': '谢语可靠'}, {'text': '流星雨', 'basis': '礼物词面可靠'}],
                      evidence_refs=['audio'], literal_uncertainty='昵称无法逐字确认')
    output, repairs, _, _ = run(tmp_path, monkeypatch, response=response, text=srt, context={})
    assert '谢谢流星雨' in output and len(repairs) == 1
    assert repairs[0]['literal_transcription'] is False
    for i, change in enumerate(({'edit_start': 0}, {'edit_end': len(text)},
                                {'reliable_fragments': []}, {'mode': 'gift_thanks_summary'})):
        assert run(tmp_path / str(i), monkeypatch, response={**response, **change}, text=srt, context={})[1] == []


def test_same_bound_raw_editorial_answer_recompiles_without_provider_or_history_mutation(tmp_path, monkeypatch):
    from src.autoslice.editorial_subtitle import adjudicate
    _, repairs, rows, _ = run(tmp_path, monkeypatch)
    finding = copy.deepcopy(rows[0])
    old = finding['exact_release_adjudication']
    old.update(status='UNCERTAIN', repaired=False, reason_code='EDITORIAL_EVIDENCE_REF_INVALID')
    old['editorial_authority'].pop('proposed_cue')
    original = json.dumps(finding, sort_keys=True)
    request = old['editorial_authority']['request']['check_request']
    def forbidden(*_args):
        pytest.fail('bound raw replay called a provider')
    result = adjudicate(SRT, finding, check_request=request, clip_context=CONTEXT,
                        entity_verifier=forbidden, judge_llm_call=forbidden, screen_read_probe=forbidden)
    assert result['repaired'] is True
    assert result['editorial_authority_replay']['provider_calls'] == 0
    assert result['editorial_authority']['proposed_cue'] == TEXT[:-2] + '流星雨'
    assert json.dumps(finding, sort_keys=True) == original
    # A changed text or context cannot inherit that old response's edit.
    for current, context in ((SRT.replace(TEXT, '别的整句'), CONTEXT), (SRT, {})):
        rejected = adjudicate(current, finding, check_request=request, clip_context=context,
                              entity_verifier=None, judge_llm_call=None, screen_read_probe=None)
        assert rejected['repaired'] is False
        assert rejected['editorial_authority_replay']['status'] == 'REJECTED'


@pytest.mark.parametrize('nickname,gift', [('陌生昵称的', '流星雨'), ('XYZ的', '比心')])
def test_gift_summary_can_omit_only_unknown_nickname_while_retaining_supported_gift(tmp_path, monkeypatch, nickname, gift):
    text = '谢谢' + nickname + gift
    srt = SRT.replace(TEXT, text)
    response = answer(mode='gift_thanks_summary', edit_start=2, edit_end=2+len(nickname), replacement='',
                      reliable_fragments=[{'text': '谢谢', 'basis': '致谢可靠'}, {'text': gift, 'basis': '现文礼物词面可靠'}],
                      evidence_refs=['audio'], gift_thanks_confirmed=True, name_sources_exhausted=True)
    output, repairs, _, _ = run(tmp_path, monkeypatch, response=response, text=srt, context={})
    assert '谢谢' + gift in output and len(repairs) == 1
    for i, change in enumerate(({'name_sources_exhausted': False}, {'gift_thanks_confirmed': False},
                                {'edit_end': len(text)}, {'reliable_fragments': []})):
        assert run(tmp_path / str(i), monkeypatch, response={**response, **change}, text=srt, context={})[1] == []


def test_faithful_local_paraphrase_preserves_clear_prefix_and_explains_it_to_fresh_review(tmp_path, monkeypatch):
    from src.autoslice.editorial_subtitle import rendering_context
    text = '我听到了，嗯哼哼'
    srt = SRT.replace(TEXT, text)
    response = answer(mode='faithful_paraphrase', edit_start=5, edit_end=len(text), replacement='哼唱',
                      reliable_fragments=[{'text': '我听到了', 'basis': '前半句清楚'}], evidence_refs=['audio'],
                      meaning_basis='已确认听到后在哼唱，次数不确定', literal_uncertainty='尾部次数不可辨')
    output, repairs, _, _ = run(tmp_path, monkeypatch, response=response, text=srt, context={})
    assert '我听到了，哼唱' in output
    authority = {}
    _register_exact_final_cpa_repairs(authority, repairs, delivery_start_ms=5000)
    explanation, cues = rendering_context(output, authority, 5000)
    assert cues == frozenset({1}) and 'faithful_paraphrase' in explanation and '不是逐字听清' in explanation
    assert rendering_context(output.replace('我听到了', '我答应你'), authority, 5000) == ('', frozenset())
    unknown = {**response, 'reliable_fragments': []}
    assert run(tmp_path / 'unknown', monkeypatch, response=unknown, text=srt, context={})[1] == []


def test_screen_name_uses_actual_frame_clock_and_pool_not_phonetic_fit(tmp_path, monkeypatch):
    from src.autoslice.screen_read_witness import SCREEN_TEXT_QUESTION, parse_screen_text_pool
    def screen(start, end):
        row = {'schema_version': 'cpa-frame-witness.v1', 'status': 'OBSERVED', 'provider': 'cpa',
               'media_path': '/source.mp4', 'frame_ms': start + 333, 'frame_sha256': 'd' * 64,
               'model': 'test-frame', 'question': SCREEN_TEXT_QUESTION,
               'answer': json.dumps({'texts': ['流星雨']}, ensure_ascii=False)}
        row['prompt_sha256'] = hashlib.sha256(json.dumps({
            'question': SCREEN_TEXT_QUESTION, 'frame_sha256': row['frame_sha256'], 'model': row['model']
        }, sort_keys=True).encode()).hexdigest()
        return {'schema_version': 'screen-read-witness.v1', 'span_start_ms': start, 'span_end_ms': end,
                'media_path': '/source.mp4', 'receipts': [row], 'pool': parse_screen_text_pool([row])}
    response = answer(mode='bound_screen_spelling', screen_index=0, evidence_refs=['audio', 'screen'])
    output, repairs, _, _ = run(tmp_path, monkeypatch, response=response, context={}, screen=screen)
    assert '流星雨' in output
    corrupt = copy.deepcopy(repairs[0]['editorial_authority'])
    corrupt['request']['screen']['receipts'][0]['frame_ms'] = 0
    with pytest.raises(ValueError):
        validate_authority(corrupt)


def test_reconstructed_foreign_body_survives_uncertain_humming_in_ordinary_edit_path(tmp_path, monkeypatch):
    # Regression shape from saved cue 61: Chinese ASR garble, two native
    # Japanese candidates sharing a reliable body but differing in humming.
    # This is a controlled transport response, not a new production verdict.
    current = '呀里弄过的事可能有等等'
    body = 'やり残したことがこの世'
    response = answer(mode='faithful_paraphrase', edit_start=0, edit_end=len(current),
                      replacement=body + '……',
                      reliable_fragments=[{'text': body, 'basis': '独立日语转写共享主体'}],
                      evidence_refs=['audio'], meaning_basis='未唱完的日语主体后接哼唱，不补完歌词',
                      literal_uncertainty='五次或六次哼唱以及连接音不确定')
    output, repairs, rows, calls = run(tmp_path, monkeypatch, response=response,
                                      text=SRT.replace(TEXT, current), context={})
    assert body + '……' in output and '哼唱' not in output
    assert len(calls) == 1 and len(repairs) == 1
    assert rows[0]['proposed_full_cue'] == body + '……'
    assert repairs[0]['literal_transcription'] is False
    # The same route cannot manufacture a reliable body when none is supported.
    unknown = {**response, 'status': 'UNRESOLVED', 'reliable_fragments': []}
    unchanged, rejected, _, _ = run(tmp_path/'conflict', monkeypatch, response=unknown,
                                   text=SRT.replace(TEXT, current), context={})
    assert unchanged == SRT.replace(TEXT, current) and rejected == []


def _empty_memory(tmp_path):
    from src.autoslice.speech_memory_ledger import SCHEMA_VERSION
    path = tmp_path/'empty-memory.json'
    path.write_text(json.dumps({'schema_version':SCHEMA_VERSION, 'entries':[]}))
    return path


def _native_context(tmp_path, command, *, event_ms=800, text='千纸鹤', sender='星***', srt=SRT):
    from src.autoslice.chat_evidence import load_chat_jsonl
    from src.autoslice.clip_context import build_clip_context
    epoch = 1786769318000
    data = ({'message': text, 'user_info': {'uname': sender}, 'id': 812}
            if command == 'SUPER_CHAT_MESSAGE' else
            {'giftName': text, 'uname': sender, 'uid': 0})
    raw = (json.dumps({'cmd': command, 'send_time': epoch + event_ms, 'data': data}, ensure_ascii=False) + '\n').encode()
    events = load_chat_jsonl(tmp_path/'native.jsonl', recording_start_ms=epoch, source_bytes=raw)
    context = build_clip_context(candidate_id='ordinary-native', spec={'date': '2026-10-14', 'pieces': []},
        draft_srt=srt, authoritative_chat=events, topic_resolution={}, session_topic_authorities=[],
        speech_memory_ledger_path=_empty_memory(tmp_path))
    return events, context


@pytest.mark.parametrize('command,gift,sender', [('SEND_GIFT', '千纸鹤', '星***'), ('COMBO_SEND', '小花花', '另***')])
def test_native_gift_ingress_reaches_typed_editorial_apply_without_invented_id(tmp_path, monkeypatch, command, gift, sender):
    events, context = _native_context(tmp_path, command, text=gift, sender=sender)
    assert len(events) == 1 and events[0].source_event_id == ''
    response = answer(replacement=gift, event_index=0, evidence_refs=['audio', 'event:0'])
    output, repairs, _, _ = run(tmp_path, monkeypatch, context=context, response=response)
    assert TEXT[:-2] + gift in output and len(repairs) == 1
    request = repairs[0]['editorial_authority']['request']
    assert request['events'][0]['source_event_id'] == ''
    assert request['events'][0]['context_row_index'] == 0
    assert request['events'][0]['source_sha256'] == events[0].source_sha256
    validate_authority(repairs[0]['editorial_authority'], clip_context=context)
    registered = {'exact_final_cpa_self_heal': {
        'schema_version': 'exact-final-cpa-self-heal-audit.v1', 'status': 'REVIEW_PENDING',
        'passes': [{'schema_version': 'exact-final-cpa-self-heal-pass.v1', 'pass_index': 1,
                    'input_srt_sha256': h(SRT), 'output_srt_sha256': h(output), 'repairs': repairs}],
    }}
    _register_exact_final_cpa_repairs(registered, repairs, delivery_start_ms=5000)
    restored = json.loads(json.dumps(registered))
    _continuation_validate_exact_final_owner(restored['entity_repairs'][0], row_index=0, chat_authority=restored)
    from src.autoslice.editorial_subtitle import rendering_context
    assert rendering_context(output, restored, 5000)[1] == frozenset({1})
    changed = copy.deepcopy(context)
    changed['structured_chat'][0]['text'] = '另一个礼物'
    with pytest.raises(ValueError, match='CONTEXT_DRIFT'):
        validate_authority(repairs[0]['editorial_authority'], clip_context=changed)
    bad_source = copy.deepcopy(context)
    bad_source['structured_chat'][0]['source_sha256'] = ''
    assert run(tmp_path/'bad-source', monkeypatch, context=bad_source, response=response)[1] == []
    masked = {**response, 'event_field': 'sender', 'replacement': sender}
    assert run(tmp_path/'masked', monkeypatch, context=context, response=masked)[1] == []


@pytest.mark.parametrize('delay', [120000, 120001, 300000, 899000, 900000])
def test_native_delayed_sc_ingress_reaches_editorial_apply(tmp_path, monkeypatch, delay):
    start = 950000
    text = '谢谢小名字的SC'
    srt = '1\n00:15:50,000 --> 00:15:52,000\n' + text + '\n'
    events, context = _native_context(tmp_path, 'SUPER_CHAT_MESSAGE', event_ms=start-delay,
                                      text='延迟念的正文', sender='小明子', srt=srt)
    response = answer(edit_start=2, edit_end=5, replacement='小明子', event_field='sender', event_index=0,
                      reliable_fragments=[{'text':'谢谢', 'basis':'感谢'}, {'text':'的SC', 'basis':'SC语境'}],
                      evidence_refs=['audio', 'event:0'])
    output, repairs, _, _ = run(tmp_path, monkeypatch, text=srt, context=context, response=response, offset=0)
    assert '谢谢小明子的SC' in output and len(repairs) == 1
    assert repairs[0]['editorial_authority']['request']['events'][0]['offset_ms'] == start-delay


def test_native_event_horizons_are_kind_specific(tmp_path):
    from src.autoslice.editorial_subtitle import _events
    from src.autoslice.chat_evidence import ChatEvidence
    from src.autoslice.clip_context import build_clip_context
    events = [ChatEvidence(kind, 0, '文字', '名字', source_sha256='a'*64)
              for kind in ('gift', 'guard', 'superchat', 'danmaku')]
    context = build_clip_context(candidate_id='window-controls', spec={}, draft_srt=SRT,
        authoritative_chat=events, topic_resolution={}, session_topic_authorities=[],
        speech_memory_ledger_path=_empty_memory(tmp_path))
    assert {e['kind'] for e in _events(context,120001,122001)} == {'guard', 'superchat'}
    assert {e['kind'] for e in _events(context,300001,302001)} == {'superchat'}
    assert _events(context,900001,902001) == []


def test_native_xml_danmaku_without_platform_id_survives_piece_ingress(tmp_path, monkeypatch):
    from src.autoslice.producer_chat_input import _piece_chat_evidence
    from src.autoslice.clip_context import build_clip_context
    xml = tmp_path/'recording.xml'
    xml.write_text('<i><d p="0.8,1,25,16777215,1782961247649,0,ea71f718,578340245">千纸鹤</d></i>')
    events = _piece_chat_evidence({'remote_media': str(tmp_path/'recording.mp4'),
                                   'danmaku_xml_local': str(xml)})
    assert len(events) == 1 and events[0].source_event_id == ''
    context = build_clip_context(candidate_id='native-xml', spec={}, draft_srt=SRT,
        authoritative_chat=events, topic_resolution={}, session_topic_authorities=[],
        speech_memory_ledger_path=_empty_memory(tmp_path))
    output, repairs, _, _ = run(tmp_path, monkeypatch, context=context,
        response=answer(replacement='千纸鹤', event_index=0, evidence_refs=['audio', 'event:0']))
    assert '千纸鹤' in output and len(repairs) == 1
    event = repairs[0]['editorial_authority']['request']['events'][0]
    assert event['kind'] == 'danmaku' and event['offset_ms'] == 800
    assert event['source_sha256'] == hashlib.sha256(xml.read_bytes()).hexdigest()
    assert event['source_event_id'] == '' and event['context_row_index'] == 0


def omission_answer(text, **changes):
    return answer(mode='nonsemantic_vocalization_omit', edit_start=0, edit_end=len(text),
                  replacement='', reliable_fragments=[], evidence_refs=['audio'],
                  meaning_basis='有声但无词义的非语言发声，仅省略显示',
                  literal_uncertainty='ASR将非语言发声写成词语',
                  nonsemantic_vocalization_confirmed=True, spoken_or_lyric_content_absent=True,
                  nonsemantic_audio_basis='音频呈清嗓或连续无词义哼声，无可辨话语或歌词', **changes)


@pytest.mark.parametrize('current', ['嗯哼哼', '谢谢', '等等等等'])
def test_nonsemantic_vocalization_omission_normal_apply_reload_and_ass(tmp_path, monkeypatch, current):
    from src.autoslice.editorial_subtitle import rendering_context, valid_display_omission_repair
    from src.autoslice.subtitle_rendering import _write_sapphire_ass_from_srt
    from src.autoslice.jingting_chunker import parse_srt_cues
    source = SRT.replace(TEXT, current) + '\n2\n00:00:03,000 --> 00:00:05,000\nまだ終わらない\n'
    output, repairs, _, _ = run(tmp_path, monkeypatch, context={}, text=source, response=omission_answer(current))
    assert len(repairs) == 1 and valid_display_omission_repair(repairs[0])
    assert [(c.start_ms,c.end_ms,c.text) for c in parse_srt_cues(output)] == [(3000,5000,'まだ終わらない')]
    assert repairs[0]['editorial_authority']['request']['audio']['target_audible'] is True
    authority = {'exact_final_cpa_self_heal': {
        'schema_version':'exact-final-cpa-self-heal-audit.v1', 'status':'REVIEW_PENDING',
        'passes':[{'schema_version':'exact-final-cpa-self-heal-pass.v1','pass_index':1,
                   'input_srt_sha256':h(source),'output_srt_sha256':h(output),'repairs':repairs}]}}
    _register_exact_final_cpa_repairs(authority, repairs, delivery_start_ms=5000)
    restored = json.loads(json.dumps(authority))
    _validate_exact_final_cpa_self_heal(restored, expected_srt_sha256=h(output), allow_pending=True)
    _continuation_validate_exact_final_owner(restored['entity_repairs'][0], row_index=0, chat_authority=restored)
    explanation, indexes = rendering_context(output, restored, 5000)
    assert 'nonsemantic_vocalization_omit' in explanation and not indexes
    assert rendering_context(source, restored, 5000) == ('', frozenset())
    srt, ass = tmp_path/'final.srt', tmp_path/'final.ass'
    srt.write_text(output)
    _write_sapphire_ass_from_srt(srt,ass)
    dialogues = [line for line in ass.read_text().splitlines() if line.startswith('Dialogue:')]
    assert len(dialogues) == 1 and 'まだ終わらない' in dialogues[0]
    assert '0:00:03.00,0:00:05.00' in dialogues[0]
    from src.autoslice.producer_text_finalization import verify_chat_authority_final_surfaces
    def verify(audit, text=output, speaker=output):
        return verify_chat_authority_final_surfaces(audit, final_text_srt=text,
            final_speaker_srt=speaker, delivery_start_ms=5000, delivery_end_ms=10000)
    assert verify(restored)
    owner = restored['entity_repairs'][0]
    assert owner['final_display_omission_empty_text_window'] is True
    assert owner['survived_final_text_srt'] and owner['survived_final_speaker_srt']
    assert not verify(copy.deepcopy(restored), source)
    assert not verify(copy.deepcopy(restored), speaker=source)
    for drift in ('clock', 'receipt', 'after', 'audio'):
        corrupted = copy.deepcopy(restored)
        row = corrupted['entity_repairs'][0]
        if drift == 'clock': row['matched_start_ms'] += 1
        elif drift == 'receipt': row['editorial_authority']['raw_completion'] += 'tamper'
        elif drift == 'after': row['after'] = ['任意新文字']
        else: row['editorial_authority']['request']['audio']['target_audible'] = False
        assert not verify(corrupted)


@pytest.mark.parametrize('body', ['やり残したことがこの世', '来吧，来点放……', '我听到了'])
def test_mixed_vocalization_omission_preserves_reliable_body(tmp_path, monkeypatch, body):
    current = body + '嗯哼哼'
    response = omission_answer(current)
    response.update(edit_start=len(body), reliable_fragments=[{'text':body,'basis':'可靠歌词或未完成话语'}])
    output, repairs, _, _ = run(tmp_path,monkeypatch,context={},text=SRT.replace(TEXT,current),response=response)
    assert len(repairs) == 1 and repairs[0]['after'] == body
    assert body in output and '嗯哼哼' not in output
    # An omission verdict cannot consume its own independently reliable words.
    response.update(edit_start=0)
    unchanged, rejected, _, _ = run(tmp_path/'bad',monkeypatch,context={},text=SRT.replace(TEXT,current),response=response)
    assert unchanged == SRT.replace(TEXT,current) and not rejected


@pytest.mark.parametrize('change', [
    {'spoken_or_lyric_content_absent':False}, {'nonsemantic_vocalization_confirmed':False},
    {'nonsemantic_audio_basis':''}, {'mode':'faithful_paraphrase'},
    {'status':'UNRESOLVED'}, {'reliable_fragments':[{'text':'歌詞','basis':'确实有词义'}]},
])
def test_omission_requires_positive_nonsemantic_evidence(tmp_path, monkeypatch, change):
    response = omission_answer('歌詞')
    response.update(change)
    source = SRT.replace(TEXT,'歌詞')
    output, repairs, _, _ = run(tmp_path,monkeypatch,context={},text=source,response=response)
    assert output == source and not repairs


def test_older_editorial_receipt_remains_valid_after_omission_prompt_extension(tmp_path, monkeypatch):
    from src.autoslice import editorial_subtitle
    with monkeypatch.context() as previous:
        previous.setattr(editorial_subtitle, '_editorial_prompt', lambda request:
            editorial_subtitle.PROMPT + json.dumps(request, ensure_ascii=False, sort_keys=True))
        output, repairs, _, _ = run(tmp_path,previous)
    validate_repair(json.loads(json.dumps(repairs[0])))
    assert '流星雨' in output


def test_observed_meaning_and_interrupted_foreign_body_use_existing_editorial_lane(tmp_path, monkeypatch):
    # The observation is supplied, not a claim that this test hears a cup.
    for index, (current, body) in enumerate([
        ('喝不喝水', '喝口水'),
        ('亚里诺口西塔口头等等等等', 'やり残したこと'),
        ('来吧，来点放完', '来吧，来点放……'),
    ]):
        response = answer(mode='faithful_paraphrase', edit_start=0,edit_end=len(current),replacement=body,
            reliable_fragments=[{'text':body,'basis':'受控独立观察确认的含义或真实未完成话语'}],
            evidence_refs=['audio'], meaning_basis='保留已确认含义，不追填充次数或补全中断',
            literal_uncertainty='ASR字形不可靠')
        output, repairs, _, calls = run(tmp_path/str(index),monkeypatch,context={},text=SRT.replace(TEXT,current),response=response)
        assert repairs[0]['after'] == body and body in output and len(calls)==1
        assert '环境声仅支持相称的动作/语义' in calls[0]
