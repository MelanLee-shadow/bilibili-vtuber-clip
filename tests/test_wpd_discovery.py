from __future__ import annotations

from dataclasses import replace
import inspect
import json

import pytest

from src.autoslice.discovery_channels import (
    ASR_CHANNELS, CHANNELS, Channel, ClosedFinding, ClosedFindings, DiscoveryBatch,
    ExhaustionEvidence, Nomination, SourceIdentity, batch_from_rows,
    nominations_from_rows, observation_nominations, task_rows, union_discovery,
    validate_terminal_choice,
)
from src.autoslice.scan_coverage import ScanCoverage, cue_versions
from src.autoslice.witness_disagreement import NativeSegment, find_witness_disagreements
from src.autoslice.final_review_auditor import audit_final_subtitles


@pytest.fixture(autouse=True)
def per_finding_discovery_mode(monkeypatch):
    """WP-D contracts exercise the explicitly selected migration path."""
    monkeypatch.setenv('AUTOSLICE_FINAL_REVIEW_MODE', 'per_finding')


def srt(*texts):
    return '\n\n'.join(f'{i}\n00:00:{i*3:02d},000 --> 00:00:{i*3+2:02d},000\n{text}'
        for i, text in enumerate(texts, 1)) + '\n'


def row(cue=1, proposal=None):
    return {'cue': cue, 'kind': 'context', 'repair_class': 'phonetic',
        'proposed_full_cue': proposal, 'why': '需要核对当前整句',
        'content_risk': 'meaning_reversal', 'risk_basis': '当前整句无法确定原意'}


def nomination(channel, cue=1, proposal=None, provider='', model='', reading='', obs='obs'):
    return Nomination(channel, cue, json.dumps(row(cue, proposal), ensure_ascii=False), obs,
        provider, model, reading)


SOURCE = SourceIdentity('clip', ('a' * 64,))


def batch(*noms):
    return DiscoveryBatch(tuple(noms), SOURCE, 10000)


def test_registry_covers_every_channel():
    assert {c.channel for c in CHANNELS} == set(Channel)
    assert ASR_CHANNELS == {Channel.FULL_TRANSCRIPT, Channel.RANGE_TRANSCRIPT, Channel.POSTBURN}


def test_n_lanes_union_by_physical_question_not_label_or_proposal():
    text = srt('我说的话', '下一句话')
    first = nomination(Channel.TEXT, obs='text-lane-1')
    second = nomination(Channel.TEXT, proposal='她说的话', obs='text-lane-2')
    third = nomination(Channel.BLIND_SHORT, obs='short-witness')
    result = union_discovery(text, batch(first, second, third))
    assert len(result.tasks) == 1
    task, = result.tasks
    assert task.channels == (Channel.TEXT, Channel.BLIND_SHORT)
    assert task.observation_ids == ('short-witness', 'text-lane-1', 'text-lane-2')
    assert not task.asr_only
    assert union_discovery(text, batch(third, second, first)) == result


def test_scope_limits_all_channels():
    result = union_discovery(srt('一句', '二句'), batch(
        nomination(Channel.TEXT, 1), nomination(Channel.BLIND_SHORT, 2)), scope_cues=frozenset({2}))
    assert [t.cue_index for t in result.tasks] == [2]


def test_closed_ledger_same_material_excludes_and_new_observation_reopens():
    text = srt('一句', '二句')
    original = batch(nomination(Channel.TEXT))
    task, = union_discovery(text, original).tasks
    closed = ClosedFindings((ClosedFinding(task.question_key, task.materials_sha256),))
    assert not union_discovery(text, original, closed_ledger=closed).tasks
    assert union_discovery(text, batch(nomination(Channel.TEXT, obs='new')), closed_ledger=closed).tasks


def test_unrelated_cue_patch_does_not_rename_question():
    text = srt(*(['一句'] * 10))
    result = union_discovery(text, batch(nomination(Channel.TEXT)))
    changed = union_discovery(srt(*(['一句'] * 9 + ['改字'])), batch(nomination(Channel.TEXT)))
    assert result.tasks[0].question_key == changed.tasks[0].question_key
    assert result.tasks[0].materials_sha256 == changed.tasks[0].materials_sha256


def test_single_asr_does_not_open_but_enriches_non_asr_task():
    asr = nomination(Channel.FULL_TRANSCRIPT, proposal='他来吃饭', provider='mai', model='mai', reading='chi')
    result = union_discovery(srt('他来喝水'), batch(asr))
    assert not result.tasks and result.materials_only == (asr,)
    result = union_discovery(srt('他来喝水'), batch(asr, nomination(Channel.TEXT)))
    task, = result.tasks
    assert not task.asr_only and asr in task.nominations
    candidate = next(r for r in task_rows(result) if r.get('decision_authority'))
    assert candidate['decision_authority'] == 'NONE_DISCOVERY_ONLY'
    assert candidate['force_acoustic'] is True


@pytest.mark.parametrize('provider,model,reading,opens', [
    ('mai', 'mai', 'chi', False), ('moss', 'mai', 'chi', False),
    ('moss', 'moss', 'he', False), ('moss', 'moss', 'chi', True),
])
def test_asr_agreement_requires_two_independent_matching_readings(provider, model, reading, opens):
    a = nomination(Channel.FULL_TRANSCRIPT, proposal='他来吃饭', provider='mai', model='mai', reading='chi', obs='mai')
    b = nomination(Channel.RANGE_TRANSCRIPT, proposal='他来吃饭', provider=provider, model=model, reading=reading, obs='moss')
    result = union_discovery(srt('他来喝水'), batch(a, b))
    assert bool(result.tasks) is opens
    if opens:
        task, = result.tasks
        assert task.asr_only
        for row_value in task_rows(result):
            assert row_value['decision_authority'] == 'NONE_DISCOVERY_ONLY'
        with pytest.raises(ValueError, match='ASR_ONLY_CANNOT_SUSTAIN'):
            validate_terminal_choice(task, 'UNRESOLVABLE', ExhaustionEvidence())
        with pytest.raises(ValueError, match='ASR_ONLY_CANNOT_SUSTAIN'):
            validate_terminal_choice(task, 'UNRESOLVABLE', ExhaustionEvidence(('mai', 'moss'), True, True, True, True))
        validate_terminal_choice(task, 'UNRESOLVABLE', ExhaustionEvidence(('different-listen-model',), True, True, True, True))
        for choice in ('CURRENT', 'PROPOSED', 'CONTEXT_FIT'):
            validate_terminal_choice(task, choice, ExhaustionEvidence())


def test_asr_agreeing_with_subtitle_cannot_open():
    a = nomination(Channel.FULL_TRANSCRIPT, proposal='他来喝水', provider='mai', model='mai', reading='he', obs='mai')
    b = replace(a, provider='moss', model='moss', observation_id='moss')
    assert not union_discovery(srt('他来喝水'), batch(a, b)).tasks


def test_coverage_patch_invalidates_changed_cue_and_its_context_only():
    old = cue_versions(srt('一句', '二句', '三句', '四句'))
    coverage = ScanCoverage('source-sha', old)
    for channel in ('A', 'B', 'C0'):
        coverage = coverage.record(channel=channel, scan_id='first', scope_cues=frozenset({1, 2, 3, 4}),
            dependencies=((1, (1, 2)), (2, (2,))))
    assert coverage.gaps(('A', 'B', 'C0')) == ()
    changed = cue_versions(srt('一句', '改字', '三句', '四句'))
    revised = coverage.revise(changed, affected_cues=frozenset({2}))
    assert {g.cue_index for g in revised.gaps(('A', 'B', 'C0'))} == {1, 2}
    assert len(coverage.receipts) == 12  # old ledger remains immutable
    for channel in ('A', 'B', 'C0'):
        revised = revised.record(channel=channel, scan_id='targeted', scope_cues=frozenset({1, 2}))
    assert not revised.gaps(('A', 'B', 'C0'))


def test_coverage_never_uses_old_byte_receipt_after_clock_or_text_change():
    old = cue_versions(srt('一句'))
    ledger = ScanCoverage('src', old).record(channel='A', scan_id='first', scope_cues=frozenset({1}))
    changed = (replace(old[0], start_ms=old[0].start_ms + 1),)
    assert ledger.revise(changed, affected_cues=frozenset()).gaps(('A',))


def test_scope_only_auditor_and_no_prior_list_priming():
    prompts = []
    source = srt('一句', '二句')
    def answer(prompt):
        prompts.append(prompt)
        return json.dumps({'findings': [row(1), row(2)]}, ensure_ascii=False)
    findings = audit_final_subtitles(source, llm_call=answer, extract_json=json.loads,
        scope_cues=frozenset({2}), discovery_batch=batch_from_rows([], context=None))
    assert [f['cue_index'] for f in findings] == [2]
    assert all('本轮需逐项重新评估风险的既有疑点' not in p for p in prompts)
    assert 'extra_raw_findings' in inspect.signature(audit_final_subtitles).parameters


def test_observation_conflict_is_a_task_even_when_text_reviewer_returns_empty():
    source = srt('没有任何了解')
    supplied = batch(nomination(Channel.BLIND_SHORT, obs='bound-blind-receipt'))
    def answer(prompt):
        if '本次不授权改字、删字或听音调度' in prompt:
            return json.dumps({'findings': [row(1)]}, ensure_ascii=False)
        return '{"findings":[]}'
    findings = audit_final_subtitles(source, llm_call=answer, extract_json=json.loads, discovery_batch=supplied)
    assert findings[0]['discovery_task']['channels'] == ['B']


@pytest.mark.parametrize('foreign', ['すごい', '하세요 봐요'])
def test_script_mismatch_is_rejected_for_entire_observation(foreign):
    scan = observation_nominations(srt('因为他们学校的辩论队非常厉害'), provider='gemini_api', model='gemini',
        observation_id='obs', segments=(NativeSegment(0, 10000, foreign),), window_ms=(0, 10000),
        source_offset_ms=0, channel=Channel.RANGE_TRANSCRIPT, expected_scripts=frozenset({'han'}))
    assert not scan.nominations
    assert 'WITNESS_SCRIPT_MISMATCH' in scan.rejection_reasons


def test_window_edge_rejects_narrow_but_wide_keeps_team_difference():
    source = srt('因为他们学校的辩论', '辩论、论非常厉害')
    narrow = observation_nominations(source, provider='mai', model='mai', observation_id='narrow',
        segments=(NativeSegment(3000, 5000, '因为他们学校的变'),), window_ms=(3000, 5000),
        source_offset_ms=0, channel=Channel.RANGE_TRANSCRIPT)
    assert not narrow.nominations
    assert 'NEAR_WINDOW_EDGE' in narrow.rejection_reasons
    wide = observation_nominations(source, provider='mai', model='mai', observation_id='wide',
        segments=(NativeSegment(2000, 10000, '因为他们学校的辩论辩论队非常厉害'),),
        window_ms=(2000, 10000), source_offset_ms=0, channel=Channel.FULL_TRANSCRIPT)
    assert 2 in {n.cue_index for n in wide.nominations}


def test_traditional_simplified_aligns_without_a_partial_translation_table():
    source = srt('辩论队非常厉害')
    assert not find_witness_disagreements(source, '', intro_offset_ms=0,
        native_segments=(NativeSegment(0, 10000, '辯論隊非常厲害'),), observation_window_ms=(0, 10000))


def test_same_bytes_new_dependency_relation_invalidates_previous_coverage():
    versions = cue_versions(srt('一句', '二句', '三句'))
    ledger = ScanCoverage('src', versions).record(channel='A', scan_id='first',
        scope_cues=frozenset({1, 2, 3}))
    revised = ledger.revise(versions, affected_cues=frozenset(),
        dependencies=((1, (1, 3)), (2, (2,)), (3, (1, 3))))
    assert {g.cue_index for g in revised.gaps(('A',))} == {1, 3}


def test_prefetch_uses_existing_owner_and_real_source_identity(monkeypatch, tmp_path):
    from src.autoslice import delivery_fast_path as fast
    calls = []
    def observe(**kwargs):
        calls.append(kwargs)
        return [{'status': 'OBSERVED', 'provider': kwargs['provider']}]
    observe.available_providers = ('native', 'mai', 'moss', 'gemini')
    def verifier(_):
        pytest.fail('C0 must use range owner, never narrow direct witness')
    verifier.observe_range_context = observe
    verifier.span_termination_source_identity = lambda: 'a' * 64
    monkeypatch.setattr(fast, 'discover_microcue_findings', lambda *a, **k: ([], {'status': 'PASS'}))
    monkeypatch.setattr(fast, 'merge_restatement_priority_findings', lambda text, rows, audit, **k: (rows, audit))
    rows, audit = fast.discover_priority_findings(srt('一句', '二句'), timeline_offset_ms=10000,
        entity_verifier=verifier, out_root=tmp_path, cid='clip')
    assert not rows and audit['fullclip_discovery_status'] == 'OBSERVED'
    assert sorted(c['provider'] for c in calls) == ['mai', 'moss']
    assert all(c['range_witness_request']['cue_indexes'] == [1, 2] for c in calls)
    assert all(c['check_request']['clip_context_binding']['source_media_sha256s'] == ['a' * 64] for c in calls)
    assert calls[0]['range_witness_request'] == calls[1]['range_witness_request']


def test_two_native_readings_reach_auditor_as_candidates_without_votes():
    source = srt('他来喝水')
    scans = [observation_nominations(source, provider=p, model=p, observation_id=('a' if p == 'mai' else 'b') * 64,
        segments=(NativeSegment(0, 10000, '他来吃饭'),), window_ms=(0, 10000), source_offset_ms=0,
        channel=Channel.FULL_TRANSCRIPT) for p in ('mai', 'moss')]
    supplied = DiscoveryBatch(tuple(n for scan in scans for n in scan.nominations), SOURCE, 0)
    calls = []
    def answer(prompt):
        calls.append(prompt)
        if '本次不授权改字、删字或听音调度' in prompt:
            return json.dumps({'findings': [row()]}, ensure_ascii=False)
        return '{"findings":[]}'
    typed_results = []
    findings = audit_final_subtitles(source, llm_call=answer, extract_json=json.loads,
        source_media_timeline_offset_ms=0, discovery_batch=supplied, discovery_result_sink=typed_results.append)
    assert findings and len(calls) == 2
    assert len(typed_results) == 1 and len(typed_results[0].tasks) == 1
    assert typed_results[0].tasks[0].asr_only
    assert all(f['decision_authority'] == 'NONE_DISCOVERY_ONLY' for f in findings)
    assert all(f['candidate_provenance']['decision_authority'] == 'NONE_DISCOVERY_ONLY' for f in findings)
    assert len({f['discovery_task']['physical_question_sha256'] for f in findings}) == 1


def test_lone_postburn_asr_is_material_and_does_not_create_block(monkeypatch, tmp_path):
    from tests.test_witness_disagreement import _run_postburn, _postburn_llm
    finalization, record, audit, final_srt, kwargs = _run_postburn(tmp_path, monkeypatch)
    calls = []
    _postburn_llm(monkeypatch, calls)
    configured = finalization.build_final_review_llm_call()
    def no_independent_nomination(prompt):
        calls.append(prompt)
        return '{"findings":[]}'
    no_independent_nomination.__dict__.update(configured.__dict__)
    monkeypatch.setattr(finalization, 'build_final_review_llm_call', lambda: no_independent_nomination)
    finalization._run_postburn_subtitle_audio_review(**kwargs)
    receipt = audit['postburn_subtitle_audio_review']
    assert receipt['release_gate'] == 'PASS'
    assert receipt['findings'] == []
    assert receipt['witness_disagreement']['material_only_nomination_ids']
    assert receipt['witness_disagreement']['open_nomination_ids'] == []


def test_label_and_reviewer_prose_do_not_reopen_a_closed_question():
    text = srt('一句')
    original = nominations_from_rows([row()], channel=Channel.TEXT)
    changed = nominations_from_rows([{**row(), 'kind': 'nonword', 'why': '另一路的理由措辞'}], channel=Channel.TEXT)
    before, = union_discovery(text, batch(*original)).tasks
    after, = union_discovery(text, batch(*changed)).tasks
    assert before.materials_sha256 == after.materials_sha256
    closed = ClosedFindings((ClosedFinding(before.question_key, before.materials_sha256),))
    assert not union_discovery(text, batch(*changed), closed_ledger=closed).tasks


def test_gemini_api_and_agy_same_model_are_not_independent_asrs():
    a = nomination(Channel.RANGE_TRANSCRIPT, proposal='他来吃饭', provider='gemini_api',
        model='models/gemini-3.6-flash', reading='chi', obs='first')
    b = replace(a, provider='agy', model='gemini-3.6-flash', observation_id='second')
    assert not union_discovery(srt('他来喝水'), batch(a, b)).tasks


def test_boolean_cue_scope_is_rejected():
    with pytest.raises(ValueError, match='DISCOVERY_SCOPE_INVALID'):
        union_discovery(srt('一句'), batch(nomination(Channel.TEXT)), scope_cues=frozenset({True}))


def test_auditor_deduplicates_question_but_preserves_independent_channel_provenance():
    source = srt('一句')
    nominated = row()
    supplied = DiscoveryBatch(nominations_from_rows([nominated], channel=Channel.BLIND_SHORT), SOURCE, 0)
    findings = audit_final_subtitles(source, llm_call=lambda _: json.dumps({'findings': [nominated]}),
        extract_json=json.loads, discovery_batch=supplied)
    assert findings
    assert all(f['discovery_task']['channels'] == ['A', 'B'] for f in findings)
    assert len({f['discovery_task']['physical_question_sha256'] for f in findings}) == 1


def test_script_mismatch_is_excluded_from_context_projection(monkeypatch):
    from src.autoslice import discovery_channels as channels
    from src.autoslice import producer_text_pipeline as pipeline
    from tests.test_producer_text_pipeline_final_review import _adapters, _correction_pass
    captured = []
    def scan(*args, channel, **kwargs):
        return channels.ObservationScan((), ('WITNESS_SCRIPT_MISMATCH',), ('a' * 64,)) if channel == Channel.RANGE_TRANSCRIPT else channels.ObservationScan((), ())
    def context(*args, range_audio, **kwargs):
        captured.append(list(range_audio))
        return '', {}
    monkeypatch.setattr(channels, 'validated_observation_nominations', scan)
    monkeypatch.setattr(pipeline, '_bound_source_evidence_context', context)
    monkeypatch.setattr(pipeline, 'clip_context_prompt_text', lambda _: '')
    monkeypatch.setattr(pipeline, 'discover_candidate_pronoun_findings', lambda *a, **k: ([], {}))
    monkeypatch.setattr(pipeline, 'audit_final_subtitles', lambda *a, **k: [])
    pipeline._run_exact_final_release_review(srt_text=srt('一句'), correction_audit=_correction_pass(),
        adapters=_adapters(), authoritative_chat=(), selection_hook='', clip_context={},
        prior_range_acoustic_observations=[{'artifact_bindings': {'raw_response_sha256': 'sha256:' + 'a' * 64}}],
        final_review_llm=lambda _: '{"findings":[]}', pronoun_audit_llm=lambda _: '{}')
    assert captured == [[]]


def test_bound_language_scan_can_preserve_real_japanese_over_han_surface():
    from src.autoslice.discovery_channels import language_spans_from_audit
    spans = language_spans_from_audit({'scan': {'foreign_spans': [
        {'start_ms': 0, 'end_ms': 10000, 'language': 'ja'}]}})
    allowed = frozenset({'han'}) | spans[0].scripts
    scan = observation_nominations(srt('因为他们学校的辩论队非常厉害'), provider='mai', model='mai',
        observation_id='obs', segments=(NativeSegment(0, 10000, 'すごい'),), window_ms=(0, 10000),
        source_offset_ms=0, channel=Channel.FULL_TRANSCRIPT, expected_scripts=allowed)
    assert 'WITNESS_SCRIPT_MISMATCH' not in scan.rejection_reasons


@pytest.mark.parametrize('fullclip_available,blind_status,blind_current,complete', [
    (True, 'PASS', True, True), (False, 'PASS', True, False),
    (True, 'PARTIAL', True, False), (True, 'PASS', False, False),
])
def test_producer_coverage_requires_current_completed_channel_receipts(
    monkeypatch, fullclip_available, blind_status, blind_current, complete,
):
    import hashlib
    from src.autoslice import discovery_channels as channels
    from src.autoslice import producer_text_pipeline as pipeline
    from src.autoslice import audio_language_discovery as language
    from tests.test_producer_text_pipeline_final_review import _adapters, _correction_pass
    monkeypatch.setattr(channels, 'validated_observation_nominations', lambda *a, **k: channels.ObservationScan((), ()))
    monkeypatch.setattr(language, 'current_language_discovery_context', lambda *a, **k: ('', []))
    monkeypatch.setattr(pipeline, '_bound_source_evidence_context', lambda *a, **k: ('', {}))
    monkeypatch.setattr(pipeline, 'clip_context_prompt_text', lambda _: '')
    monkeypatch.setattr(pipeline, 'discover_candidate_pronoun_findings', lambda *a, **k: ([], {'status': 'PASS'}))
    monkeypatch.setattr(pipeline, 'audit_final_subtitles', lambda *a, **k: [])
    receipt = pipeline._run_exact_final_release_review(srt_text=srt('一句'), correction_audit=_correction_pass(),
        adapters=_adapters(), authoritative_chat=(), selection_hook='',
        clip_context={'candidate_id': 'clip', 'pieces': [{'source_media_sha256': 'a' * 64}]},
        verified_authority_audit={'audio_language_discovery_audit': {'scan': {'status': 'OBSERVED'}}},
        acoustic_discovery_audit={'status': blind_status, 'restatement_discovery_status': 'PASS',
            'srt_sha256': 'sha256:' + hashlib.sha256((srt('一句') if blind_current else srt('旧句')).encode()).hexdigest(),
            'fullclip_discovery_status': 'OBSERVED' if fullclip_available else 'INCOMPLETE',
            'fullclip_discovery_observations': [{'provider': 'mai'}, {'provider': 'moss'}] if fullclip_available else []},
        final_review_llm=lambda _: '{"findings":[]}', pronoun_audit_llm=lambda _: '{}')
    assert receipt['scan_coverage_complete'] is complete


def test_open_ledger_transport_cannot_turn_asr_origins_into_a_non_asr_vote():
    first = nomination(Channel.OPEN_LEDGER, proposal='他来吃饭', provider='mai', model='mai', reading='chi', obs='mai')
    first = replace(first, origin_channels=(Channel.FULL_TRANSCRIPT,))
    second = replace(first, provider='moss', model='moss', observation_id='moss')
    assert not union_discovery(srt('他来喝水'), batch(first)).tasks
    task, = union_discovery(srt('他来喝水'), batch(first, second)).tasks
    assert task.channels == (Channel.FULL_TRANSCRIPT,) and task.asr_only
    with pytest.raises(ValueError, match='ASR_ONLY_CANNOT_SUSTAIN'):
        validate_terminal_choice(task, 'UNRESOLVABLE', ExhaustionEvidence())


def test_postburn_asr_on_independently_opened_text_question_still_blocks(monkeypatch, tmp_path):
    from tests.test_witness_disagreement import _run_postburn
    finalization, record, audit, final_srt, kwargs = _run_postburn(tmp_path, monkeypatch)
    def answer(_prompt):
        return json.dumps({'findings': [row(4)]}, ensure_ascii=False)
    answer.cpa_cache_identity = {'transport': 'cpa_command', 'models': ['gpt-6-sol'], 'effort': 'medium'}
    answer.provider_runtime_binding = {'provider_transport': 'runtime_cpa'}
    monkeypatch.setattr(finalization, 'build_final_review_llm_call', lambda: answer)
    with pytest.raises(SystemExit, match='POSTBURN_SUBTITLE_AUDIO_REVIEW_BLOCKED'):
        finalization._run_postburn_subtitle_audio_review(**kwargs)
    receipt = audit['postburn_subtitle_audio_review']
    assert receipt['status'] == 'FLAGGED'
    assert any('I' in f['discovery_task']['channels'] for f in receipt['findings'])


@pytest.mark.parametrize('relisten_model', ['models/MAI:latest', ' ', 'models/'])
def test_relisten_model_alias_or_absence_does_not_meet_the_different_model_requirement(relisten_model):
    first = nomination(Channel.FULL_TRANSCRIPT, proposal='他来吃饭', provider='mai', model='mai', reading='chi', obs='mai')
    second = replace(first, provider='moss', model='moss', observation_id='moss')
    task, = union_discovery(srt('他来喝水'), batch(first, second)).tasks
    with pytest.raises(ValueError, match='ASR_ONLY_CANNOT_SUSTAIN'):
        validate_terminal_choice(task, 'UNRESOLVABLE', ExhaustionEvidence((relisten_model,), True, True, True, True))


def test_different_homophonic_asr_word_guesses_are_not_agreement():
    first = nomination(Channel.FULL_TRANSCRIPT, proposal='他来河水', provider='mai', model='mai', reading='he-shui', obs='mai')
    second = nomination(Channel.FULL_TRANSCRIPT, proposal='他来喝水', provider='moss', model='moss', reading='he-shui', obs='moss')
    assert not union_discovery(srt('他来吃饭'), batch(first, second)).tasks


@pytest.mark.parametrize('start,end,heard', [
    (30000, 40000, 'すごい'),  # no overlapping cue: still reject the observation
    (0, 10000, '学校すごい'),  # shared Han does not authorize kana
])
def test_script_rejection_precedes_alignment_and_excludes_whole_observation(start, end, heard):
    scan = observation_nominations(srt('学校的辩论队'), provider='mai', model='mai',
        observation_id='unsupported-script', segments=(NativeSegment(start, end, heard),),
        window_ms=(start, end), source_offset_ms=0, channel=Channel.FULL_TRANSCRIPT)
    assert not scan.nominations
    assert scan.rejection_reasons == ('WITNESS_SCRIPT_MISMATCH',)
    assert scan.excluded_context_ids == ('unsupported-script',)


def test_a_opening_survives_equal_single_asr_material_and_projects_one_candidate():
    source = srt('他来喝水')
    proposal = row(proposal='他来吃饭')
    proposal['suspect'] = '喝水'
    native = {**proposal, 'candidate_provenance': {
        'kind': 'subtitle_audio_witness_disagreement', 'provider': 'bcut',
        'model': 'bcut', 'reading_key': 'chi-fan', 'observation_id': 'bcut-observation'}}
    supplied = DiscoveryBatch(nominations_from_rows([native], channel=Channel.POSTBURN), SOURCE)
    captured = []
    output = audit_final_subtitles(source, llm_call=lambda _: json.dumps({'findings': [proposal]}),
        extract_json=json.loads, discovery_batch=supplied, discovery_result_sink=captured.append)
    assert len(captured) == 1 and len(captured[0].tasks) == 1
    assert captured[0].tasks[0].channels == (Channel.TEXT, Channel.POSTBURN)
    assert len(captured[0].tasks[0].nominations) == 2
    assert len(output) == 1
    assert output[0]['decision_authority'] == 'NONE_DISCOVERY_ONLY'
    assert output[0]['discovery_task']['asr_only'] is False


def test_legacy_keywords_are_explicit_during_dual_mode_migration(monkeypatch):
    from src.autoslice import final_review_auditor as auditor
    assert not hasattr(auditor, '_merged_raw_findings')
    assert not any(p.kind == inspect.Parameter.VAR_KEYWORD
        for p in inspect.signature(auditor.audit_final_subtitles).parameters.values())
    assert 'extra_raw_findings' in inspect.signature(auditor.audit_final_subtitles).parameters
    monkeypatch.delenv('AUTOSLICE_FINAL_REVIEW_MODE')
    assert auditor.audit_final_subtitles(srt('一句'), llm_call=lambda _: '{"findings":[]}',
        extract_json=json.loads, extra_raw_findings=[]) == []


def test_scan_strength_is_the_registered_channel_set_with_one_text_lane():
    from src.autoslice.discovery_channels import DEFAULT_TEXT_LANES, SCAN_CHANNELS, scan_channels
    assert DEFAULT_TEXT_LANES == 1
    assert SCAN_CHANNELS == tuple(s.channel for s in CHANNELS if s.first_scan)
    assert scan_channels() == ('A', 'B', 'C0', 'C1', 'D', 'E', 'F', 'G', 'H')


def test_cold_scan_call_reader_never_falls_back_to_live_call():
    from src.autoslice.discovery_channels import _RecordedCall, _ScanCalls
    saved = (_RecordedCall('A', '{"prompt":"原始问题"}', '"原始回答"'),)
    reader = _ScanCalls(saved)
    def forbidden():
        pytest.fail('cold validation must not call a live model')
    assert reader.call('A', {'prompt': '原始问题'}, forbidden) == '原始回答'
    assert reader.retained() == saved
    with pytest.raises(ValueError, match='DISCOVERY_COLD_CALL_MISSING_OR_AMBIGUOUS'):
        reader.call('A', {'prompt': '另一个问题'}, forbidden)
    with pytest.raises(ValueError, match='DISCOVERY_COLD_CALL_SET_DRIFT'):
        _ScanCalls(saved).retained()


def test_discovery_only_port_does_not_enter_batch_adjudication(monkeypatch):
    from src.autoslice import producer_text_pipeline as pipeline
    from tests.test_producer_text_pipeline_final_review import _adapters, _correction_pass
    monkeypatch.setattr(pipeline, 'clip_context_prompt_text', lambda _: '')
    monkeypatch.setattr(pipeline, 'discover_candidate_pronoun_findings', lambda *a, **k: ([], {}))
    monkeypatch.setattr(pipeline, 'audit_final_subtitles', lambda *a, **k: [])
    def forbidden(*args, **kwargs):
        pytest.fail('discovery must not adjudicate or resolve a batch')
    monkeypatch.setattr(pipeline, 'resolve_verified_source_truth_findings', forbidden)
    monkeypatch.setattr(pipeline, 'adjudicate_exact_release_findings', forbidden)
    receipt = pipeline._run_exact_final_release_review(srt_text=srt('一句'),
        correction_audit=_correction_pass(), adapters=_adapters(), authoritative_chat=(),
        selection_hook='', clip_context={}, discovery_only=True)
    assert receipt['status'] == 'DISCOVERY_COMPLETE'
    assert receipt['release_gate'] == 'BLOCK'
    assert receipt['findings'] == []


def test_exact_coverage_reads_the_registry_instead_of_its_own_channel_list(monkeypatch):
    from src.autoslice import discovery_channels as registry, producer_text_pipeline as pipeline
    from tests.test_producer_text_pipeline_final_review import _adapters, _correction_pass
    monkeypatch.setattr(pipeline, 'clip_context_prompt_text', lambda _: '')
    monkeypatch.setattr(pipeline, 'discover_candidate_pronoun_findings', lambda *a, **k: ([], {}))
    monkeypatch.setattr(pipeline, 'audit_final_subtitles', lambda *a, **k: [])
    # A bounded registry substitute makes the completion rule observable.
    monkeypatch.setattr(registry, 'scan_channels', lambda: ('A',))
    receipt = pipeline._run_exact_final_release_review(srt_text=srt('一句'),
        correction_audit=_correction_pass(), adapters=_adapters(), authoritative_chat=(),
        selection_hook='', clip_context={'candidate_id': 'clip',
            'pieces': [{'source_media_sha256': 'a' * 64}]}, discovery_only=True)
    assert receipt['status'] == 'DISCOVERY_COMPLETE'
    assert receipt['scan_coverage_complete'] is True
    assert receipt['release_gate'] == 'BLOCK'
