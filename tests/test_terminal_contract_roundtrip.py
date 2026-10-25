"""Real request producer -> rendered prompt -> protocol reply -> cold consumers.

The transport stub answers the rendered request, never a hand-built request.
Synthetic audio/receipts use the ordinary offline acquisition producer.
"""
from copy import deepcopy
import json

import pytest

from src.autoslice import targeted_relisten as relisten
from src.autoslice.semantic_terminal_adjudication import terminal_materials, validate_terminal_answer
from tests.test_targeted_relisten_public_contract import rig, three_observations, acquire_range, no_gain_plan  # noqa: F401


def test_replay_hash_snapshot_rechecks_bytes_and_detects_restored_mtime(tmp_path):
    import hashlib
    import os
    from scripts.predeploy_contract_gauntlet import FullHashSnapshot
    from src.autoslice import independent_context_adjudication as context
    from src.autoslice.media_copy_provenance import source_reads
    path = tmp_path / 'source.bin'
    path.write_bytes(b'original')
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    with FullHashSnapshot() as snapshot, source_reads() as reads:
        assert context._file_sha256(str(path)) == expected
        assert context._file_sha256(str(path)) == expected
        assert reads[str(path)]['sha256'] == expected
        assert snapshot.hits > 0
    stamp = path.stat()
    with pytest.raises(ValueError, match='GAUNTLET_INPUT_CHANGED'):
        with FullHashSnapshot():
            assert context._file_sha256(str(path)) == expected
            path.write_bytes(b'modified')
            os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            context._file_sha256(str(path))
    path.write_bytes(b'original')
    with pytest.raises(ValueError, match='GAUNTLET_INPUT_CHANGED'):
        with FullHashSnapshot():
            assert context._file_sha256(str(path)) == expected
            # Exit must rehash even when no subsequent hash call is made.
            path.write_bytes(b'modified')


def produce(rig, reply, *, native=False, wide=()):
    _, check, finding, _ = three_observations(rig)
    _, recovery, _ = acquire_range(rig, finding, check, no_gain_plan)
    from src.autoslice.exact_final_terminal_funnel import terminal_check, review_pass_receipt
    check = terminal_check(rig.srt, finding, clip_context=rig.context, offset_ms=rig.offset_ms,
        ranges=recovery['terminal_check_request'].get('context_only_range_acoustic_observations', []))
    if native:
        import hashlib
        from src.autoslice import native_foreign_witness as foreign
        from src.autoslice import native_context_witness as native_adapter
        from src.autoslice.acoustic_witness_adjudication import build_witness_request
        bound = build_witness_request(check)
        secondary = []
        texts = [check['current_cue']] if native is True else list(native)
        for provider, transcript in zip(('mai', 'moss'), texts):
            def transcribe(audio, **kwargs):
                return {'provider': provider, 'model': 'MAI-Transcribe-2' if provider == 'mai' else 'moss-transcribe-diarize-pro',
                    'input_audio_sha256': hashlib.sha256(audio).hexdigest(),
                    'response_sha256': hashlib.sha256((provider + transcript).encode()).hexdigest(),
                    'native_segments': [{'start_ms': 0, 'end_ms': 1000, 'text': transcript}]}
            with pytest.MonkeyPatch.context() as boundary:
                boundary.setattr(foreign, 'observe_secondary', transcribe)
                observe = foreign.build_native_foreign_witness(source_media=rig.source,
                    output_dir=rig.tmp / ('native-terminal-' + provider), provider=provider)
                prepared = native_adapter._prepare_request(bound)
                receipt = observe(start_ms=prepared.physical_start_ms, end_ms=prepared.physical_end_ms)
                observed = native_adapter._native_transcript_witness(request=bound, prepared=prepared,
                    native_observation=receipt, source_sha256=rig.local.source_sha256, provider=provider)
            secondary.append({'witness_request': bound, 'verdict': observed})
        check = terminal_check(rig.srt, finding, clip_context=rig.context, offset_ms=rig.offset_ms,
            ranges=check.get('context_only_range_acoustic_observations', []),
            secondary=secondary)
    if wide:
        import hashlib
        from src.autoslice import diarized_transcription
        from src.autoslice.native_context_witness import build_independent_context_witnesses
        from src.autoslice.acoustic_witness_adjudication import build_witness_request
        from src.autoslice.jingting_chunker import parse_srt_cues
        cues = parse_srt_cues(rig.srt)
        broad = build_witness_request(check)
        broad.pop('syllable_count_hint', None)
        broad.pop('request_sha256', None)
        first, last = cues[0].start_ms, cues[2].end_ms
        broad.update(cue_indexes=[1, 2, 3], matched_start_ms=first, matched_end_ms=last,
            context_start_ms=first, context_end_ms=last)
        broad['request_sha256'] = relisten.digest(broad)
        texts = dict(zip(('mai', 'moss'), wide))
        def transcribe(audio, **kwargs):
            provider = kwargs['provider']
            transcript = texts[provider]
            start, end = check['matched_start_ms'] - first, check['matched_end_ms'] - first
            kwargs['before_request']()
            if provider == 'mai':
                from src.autoslice.mai_transcription import _parse_evidence_response
                raw = {'durationMilliseconds': last - first, 'combinedPhrases': [{'text': transcript}],
                    'phrases': [{'offsetMilliseconds': start, 'durationMilliseconds': end - start,
                        'text': transcript, 'words': [{'offsetMilliseconds': start,
                            'durationMilliseconds': end - start, 'text': transcript}]}]}
                segments, _, _ = _parse_evidence_response(raw, duration_ms=last - first)
            else:
                from src.autoslice.moss_transcription import _parse_segments
                raw = {'text': transcript, 'segments': [{'start_ms': start, 'end_ms': end,
                                                        'text': transcript, 'speaker': None}]}
                segments, _ = _parse_segments(raw, duration_ms=last - first)
            return {'provider': provider, 'model': 'MAI-Transcribe-2' if provider == 'mai' else 'moss-transcribe-diarize-pro',
                'input_audio_sha256': hashlib.sha256(audio).hexdigest(),
                'response_sha256': relisten.digest(raw), 'raw_response': raw,
                'status': 'OK', 'native_timeline': {'valid': True}, 'native_segments': segments}
        with pytest.MonkeyPatch.context() as boundary:
            boundary.setattr(diarized_transcription, 'transcribe_evidence', transcribe)
            verifier = build_independent_context_witnesses(fallback=rig.local, source_media=rig.source,
                output_dir=rig.tmp / 'wide-terminal', providers=tuple(texts))
            observed = verifier.observe_range_context(check_request=check,
                range_witness_request=broad, provider='native')
        assert observed and all(row.get('status') == 'OBSERVED' for row in observed), [
            {key: row.get(key) for key in ('status', 'reason_code', 'reason_detail')} for row in observed]
        check = terminal_check(rig.srt, finding, clip_context=rig.context, offset_ms=rig.offset_ms,
            ranges=[*check.get('context_only_range_acoustic_observations', []), *observed],
            secondary=secondary if native else [])
    receipt = review_pass_receipt(pass_index=1, srt_text=rig.srt, audit={'findings': [finding]},
        repaired_text=rig.srt, repairs=[])
    recovery = {'terminal_check_request': check, 'no_gain_proof': relisten.compile_loop_stop_no_gain(
        check=check, finding=finding, pass_receipts=[receipt])}
    captured = {}
    def call(prompt):
        from src.autoslice.structured_output import current_contract
        data = json.loads(prompt.rsplit('\n', 1)[1])
        refs = json.loads(next(line.split('：', 1)[1] for line in prompt.splitlines()
            if line.startswith('可引用编号')))
        answer = reply(data, refs)
        captured.update(prompt=prompt, data=data, refs=refs, answer=answer,
            response_schema=deepcopy(current_contract()['schema']))
        return json.dumps(answer, ensure_ascii=False)
    audit = relisten.close_with_existing_editorial(srt_text=rig.srt, finding=rig.finding,
        recovery_audit=recovery, prior=finding['exact_release_adjudication'],
        clip_context=rig.context, entity_verifier=rig.verifier, judge_llm_call=call)
    captured['request'] = audit.get('terminal_review', {}).get('request')
    return audit, captured


def c_reply(data, refs):
    return {'choice': 'CURRENT', 'grade': 'C', 'keep_route': 'C_MEANING_EQUIVALENT',
        'reason': '尾音字形不改变完整语境中的声音所指。', 'evidence_decisive': False,
        'context_can_decide': True, 'comparison_text': data.get('proposed') or data['current'],
        'evidence_refs': [key for key in refs if key in ('audio', 'context_after')],
        'meaning_equivalence': {'status': 'EQUIVALENT', 'current_meaning': '听到了声音',
            'alternative_meaning': '听到了哼唱般的声音', 'shared_meaning': '听到了那个声音',
            'uncertain_detail': '尾音字形', 'context_basis': '后一句解释哼唱。',
            'evidence_cue_ids': [2, 3]}}





def test_material_index_covers_real_prompt_and_accepts_all_supplied_segments(rig):
    audit, captured = produce(rig, c_reply)
    request = captured['request']
    materials, refs = terminal_materials(request)
    assert materials == captured['data'] and refs == captured['refs']
    assert captured['response_schema']['properties']['evidence_refs']['items']['enum'] == list(refs)
    for path in refs.values():
        value = materials
        for key in path:
            value = value[key]
    assert {path[0] for path in refs.values()} == {k for k, v in materials.items() if v is not None}
    assert validate_terminal_answer(request, {**captured['answer'], 'evidence_refs': list(refs)}) == 'CURRENT'
    with pytest.raises(ValueError, match='TERMINAL_EVIDENCE_REFS_INVALID'):
        validate_terminal_answer(request, {**captured['answer'], 'evidence_refs': ['not-provided']})


def test_both_consumers_read_producer_index_without_another_allowlist(rig, monkeypatch):
    from src.autoslice import semantic_terminal_adjudication as terminal
    from src.autoslice.response_schemas import terminal_schema
    from src.autoslice.structured_output import current_contract
    from src.autoslice.editorial_subtitle import _decision
    _, captured = produce(rig, c_reply)
    request, answer = captured['request'], captured['answer']
    original = terminal.terminal_materials
    def extended(request):
        data, refs = original(request)
        data['new_material'] = 'new source'
        refs['unseen-producer-id'] = ['new_material']
        return data, refs
    monkeypatch.setattr(terminal, 'terminal_materials', extended)
    rendered = terminal.terminal_prompt(request)
    assert json.loads(rendered.rsplit('\n', 1)[1])['new_material'] == 'new source'
    assert 'unseen-producer-id' in rendered
    answer = {**answer, 'choice': 'CONTEXT_FIT', 'grade': 'B', 'evidence_decisive': True,
        'keep_route': None,
        'status': 'RESOLVED', 'mode': 'faithful_paraphrase', 'edit_start': 5, 'edit_end': 7,
        'replacement': '哼唱', 'reliable_fragments': [{'text': '我听到了', 'basis': '原音'}],
        'meaning_basis': '后文解释声音', 'literal_uncertainty': '尾音', 'no_new_facts': True,
        'evidence_refs': ['unseen-producer-id']}
    assert validate_terminal_answer(request, answer) == 'CONTEXT_FIT'
    assert _decision(request, answer) == '我听到了，哼唱'
    expected_refs = list(extended(request)[1])
    assert terminal_schema(request)['properties']['evidence_refs']['items']['enum'] == expected_refs
    def call(prompt):
        assert prompt == rendered
        assert current_contract()['schema']['properties']['evidence_refs']['items']['enum'] == expected_refs
        return json.dumps(answer, ensure_ascii=False)
    assert json.loads(terminal.request_terminal_answer(call, rendered, request)) == answer


def test_terminal_schema_routes_follow_owner_and_legacy_policy_calls_remain_valid(rig, monkeypatch):
    from src.autoslice import semantic_terminal_adjudication as terminal
    from src.autoslice.response_schemas import terminal_schema
    from src.autoslice.semantic_terminal_adjudication import available_keep_routes
    _, captured = produce(rig, c_reply)
    request = captured['request']
    schema = terminal_schema(request)
    assert schema == captured['response_schema'] == terminal_schema(request=request)
    assert schema['properties']['keep_route']['enum'] == [*available_keep_routes(request), None]
    assert schema['properties']['current_support_observation_refs'] == {
        'type': 'array', 'items': {'type': 'string'}}
    legacy = terminal_schema(policy=request['terminal_policy'])
    assert legacy == terminal_schema(request['terminal_policy'])
    assert legacy['properties']['evidence_refs']['items'] == {'type': 'string'}
    monkeypatch.setattr(terminal, 'KEEP_ROUTES', {**terminal.KEEP_ROUTES,
        'FUTURE_KEEP_ROUTE': {'grades': ['B'], 'branch': 'offline-regression'}})
    assert 'FUTURE_KEEP_ROUTE' in terminal_schema(request)['properties']['keep_route']['enum']


def test_terminal_request_schema_checks_refs_routes_and_nullable_edit_route(rig):
    jsonschema = pytest.importorskip('jsonschema')
    Draft202012Validator = jsonschema.Draft202012Validator
    from src.autoslice.structured_output import strict_openai_schema
    _, captured = produce(rig, c_reply)
    schema = captured['response_schema']
    strict = strict_openai_schema(schema)
    for value in (schema, strict):
        Draft202012Validator.check_schema(value)
        validator = Draft202012Validator(value)
        answer = {**({key: None for key in strict['properties']} if value is strict else {}),
            **captured['answer'], 'evidence_refs': list(captured['refs']),
            'current_support_observation_refs': captured['data']['current_support_observation_refs']}
        validator.validate(answer)
        validator.validate({**answer, 'choice': 'CONTEXT_FIT', 'keep_route': None})
        for invalid in ({'evidence_refs': ['not-provided']}, {'keep_route': 'NOT_A_ROUTE'},
                {'current_support_observation_refs': [1]}):
            with pytest.raises(jsonschema.ValidationError):
                validator.validate({**answer, **invalid})


def test_b_complete_observation_keep_and_c_equivalence_use_explicit_routes(rig):
    from src.autoslice.final_review_contract import is_release_resolved_keep, is_audio_decided_keep_current
    rig.transport.pinyin = 'wo ting dao le wu wu'
    def b_reply(data, refs):
        return {**c_reply(data, refs), 'grade': 'B', 'keep_route': 'B_EVIDENCE_SUPPORTS_CURRENT',
            'evidence_decisive': True, 'evidence_refs': data['current_support_observation_refs'],
            'meaning_equivalence': None}
    audit, captured = produce(rig, b_reply)
    assert audit.get('policy_branch') == 'JUDGE_KEEPS_CURRENT', audit
    assert audit.get('contextual_terminal_handoff', {}).get('status') != 'UNRESOLVED'
    row = {**audit['rebuilt_finding'], 'exact_release_adjudication': audit}
    assert is_audio_decided_keep_current(row) and is_release_resolved_keep(row)
    forged = deepcopy(row)
    forged['exact_release_adjudication']['terminal_review']['raw_completion'] = '{}'
    assert not is_release_resolved_keep(forged)
    with pytest.raises(ValueError, match='CURRENT_OBSERVATION_SUPPORT'):
        validate_terminal_answer(captured['request'], {**captured['answer'], 'evidence_refs': ['context_after']})


def test_c_route_keeps_original_equivalence_contract_and_rejects_missing_route(rig):
    from src.autoslice.final_review_contract import is_release_resolved_keep, is_sub18_context_meaning_equivalent_keep
    audit, captured = produce(rig, c_reply)
    row = {**audit['rebuilt_finding'], 'exact_release_adjudication': audit}
    assert is_sub18_context_meaning_equivalent_keep(row) and is_release_resolved_keep(row)
    with pytest.raises(ValueError, match='KEEP_ROUTE_REQUIRED'):
        validate_terminal_answer(captured['request'], {**captured['answer'], 'keep_route': None})


def mutation_reply(data, refs):
    return {**c_reply(data, refs), 'choice': 'CONTEXT_FIT', 'keep_route': None, 'grade': 'B',
        'evidence_decisive': True, 'status': 'RESOLVED', 'mode': 'faithful_paraphrase',
        'edit_start': 0, 'edit_end': 1, 'replacement': '你', 'no_new_facts': True,
        'reliable_fragments': [{'text': '听到了', 'basis': '保留未改部分'}],
        'literal_uncertainty': '句首', 'meaning_basis': '按所给观察裁决句首',
        'comparison_text': '你听到了，呜呜'}


def test_terminal_narrow_pinyin_raises_question_without_veto_and_uses_same_projection(rig, monkeypatch):
    rig.transport.pinyin = 'wo ting dao le wu wu'
    audit, captured = produce(rig, mutation_reply)
    assert audit.get('repaired') is True, audit.get('reason_code')
    request = audit['editorial_authority']['request']
    data, _ = terminal_materials(request)
    from src.autoslice.semantic_terminal_adjudication import terminal_observation_projection
    assert data['observations'] == terminal_observation_projection(request)['observations']
    assert not data['clear_audio_constraints']
    monkeypatch.setattr(relisten, 'observation_catalogue', lambda *a: pytest.fail('guard reassembled observations'))
    relisten.protect_clear_audio(request['check_request'], request['audio'], '你听到了，呜呜',
        projection=terminal_observation_projection(request))


def test_terminal_reliable_target_audio_is_preserved_and_conflicting_edit_rejected(rig):
    from src.autoslice.editorial_subtitle import _decision
    audit, captured = produce(rig, c_reply, native=True,
        wide=('我听到了，呜呜', '我听到了，呜呜'))
    assert audit.get('policy_branch') == 'SUB18_CONTEXT_MEANING_EQUIVALENT_KEEP', audit.get('reason_code')
    request = captured['request']
    assert captured['data']['clear_audio_constraints'], request['audio_gain_termination']['projection']['rejected_observations']
    projection = request['audio_gain_termination']['projection']
    relisten.protect_clear_audio(request['check_request'], request['audio'], request['current_cue'], projection=projection)
    with pytest.raises(ValueError, match='EDITORIAL_CLEAR_AUDIO_CONFLICT'):
        _decision(request, mutation_reply(captured['data'], captured['refs']))
    forged = deepcopy(request)
    observed = next(row for row in forged['audio_gain_termination']['projection']['observations']
        if row.get('receipt_kind') == 'native_transcript')
    observed['target_text'] = '你听到了，呜呜'
    proof = forged['audio_gain_termination']
    proof['proof_sha256'] = relisten.digest({k: v for k, v in proof.items() if k != 'proof_sha256'})
    with pytest.raises(ValueError, match='OBSERVATION_DRIFT'):
        _decision(forged, mutation_reply(captured['data'], captured['refs']))


@pytest.mark.parametrize('second_reading', ['你听到了，呜呜', '你听到了呀，呜呜'])
def test_conflicting_real_native_producers_cannot_create_an_impossible_clear_audio_gate(rig, second_reading):
    from src.autoslice.editorial_subtitle import _decision
    def reply_from_guard_contract(data, refs):
        import re
        constraint = data['clear_audio_constraints'][0]
        assert constraint['reliable_fragments_contract']['ordered_nonoverlapping'] is True
        assert '互不重叠' in constraint['reliable_fragments_contract']['instructions']
        chunks = re.split(relisten.CLEAR_AUDIO_MARKER_PATTERN, constraint['text'], flags=re.I)
        reply = mutation_reply(data, refs)
        proposed = data['current'][:reply['edit_start']] + reply['replacement'] + data['current'][reply['edit_end']:]
        fragments, position = [], 0
        for chunk in chunks:
            units = relisten._clear_audio_units(chunk)
            if not units:
                continue
            # Map the guard's punctuation-neutral units onto literal text in
            # the produced sentence; never invent a punctuation-free quote.
            pattern = r'[\W_]*'.join(re.escape(unit) for unit in units)
            match = re.search(pattern, proposed[position:], re.I)
            assert match
            fragments.append({'text': match.group(), 'basis': '请求中共同清楚片段，按同一合同从左至右列出'})
            position += match.end()
        return {**reply, 'reliable_fragments': fragments}
    audit, captured = produce(rig, reply_from_guard_contract, wide=('我听到了，呜呜', second_reading))
    assert audit.get('repaired') is True, audit.get('reason_code')
    request = audit['editorial_authority']['request']
    constraints = captured['data']['clear_audio_constraints']
    assert constraints and constraints[0]['derived_from_conflicting_observations']
    # Both real provider-bound alternatives satisfy the SAME shared guard.
    from src.autoslice.semantic_terminal_adjudication import terminal_observation_projection
    projection = terminal_observation_projection(request)
    for text in ('我听到了，呜呜', second_reading):
        relisten.protect_clear_audio(request['check_request'], request['audio'], text, projection=projection)
    # Disputed prefix can be adjudicated; the uncontested body and end cannot.
    reply = mutation_reply(captured['data'], captured['refs'])
    for proposed in ('你看到了，呜呜', '你听到了，呜呜啊'):
        with pytest.raises(ValueError, match='EDITORIAL_CLEAR_AUDIO_CONFLICT'):
            _decision(request, {**reply, 'edit_start': 0, 'edit_end': len(request['current_cue']),
                'replacement': proposed})
    with pytest.raises(ValueError, match='EDITORIAL_RELIABLE_FRAGMENT_LOST'):
        _decision(request, {**captured['answer'], 'reliable_fragments': [
            {'text': '听到了', 'basis': '真实共同片段'}, {'text': '到了', 'basis': '重叠负控'}]})
    if len(captured['answer']['reliable_fragments']) > 1:
        with pytest.raises(ValueError, match='EDITORIAL_RELIABLE_FRAGMENT_LOST'):
            _decision(request, {**captured['answer'],
                'reliable_fragments': list(reversed(captured['answer']['reliable_fragments']))})





def test_c_existing_vocalization_is_not_deleted_because_native_asr_omits_it(rig):
    from src.autoslice.final_review_contract import is_release_resolved_keep
    from src.autoslice.semantic_terminal_adjudication import terminal_observation_projection
    audit, captured = produce(rig, c_reply, wide=('我听到了', '我听到了'))
    assert audit.get('policy_branch') == 'SUB18_CONTEXT_MEANING_EQUIVALENT_KEEP', audit.get('reason_code')
    assert is_release_resolved_keep({**audit['rebuilt_finding'], 'exact_release_adjudication': audit})
    request = captured['request']
    # Even with the trusted C mode, a known polarity reversal cannot pass.
    negated = '我不听到了，呜呜'
    with pytest.raises(ValueError, match='EDITORIAL_CLEAR_AUDIO_CONFLICT'):
        relisten.protect_clear_audio({**request['check_request'], 'current_cue': negated}, request['audio'], negated,
            projection=terminal_observation_projection(request), equivalent_current=True,
            policy=request['terminal_policy'], glossary_terms=request['terminal_glossary'])


def test_terminal_sound_guard_allows_homophone_spelling_but_keeps_tone_and_words(rig):
    def homophone_reply(data, refs):
        reply = mutation_reply(data, refs)
        return {**reply, 'edit_start': 0, 'edit_end': len(data['current']),
            'replacement': '我想做哪些事', 'comparison_text': '我想做哪些事',
            'reliable_fragments': [{'text': '我想做哪些', 'basis': '本次原生目标观察'}]}
    audit, _ = produce(rig, homophone_reply, native=('我想做哪些是',))
    assert audit.get('repaired') is True, audit.get('reason_code')
    for heard, proposed in [('只小的时候', '就小的时候'), ('我想买', '我想卖'), ('hello', '你好')]:
        with pytest.raises(ValueError, match='EDITORIAL_CLEAR_AUDIO_CONFLICT'):
            relisten.protect_clear_transcript(heard, proposed, tonal_homophones=True)


@pytest.mark.parametrize('negated', [False, True])
def test_real_name_keep_cannot_extend_unclear_name_slot_over_clear_polarity(tmp_path, monkeypatch, negated):
    from tests import test_targeted_relisten_public_contract as protocol
    from src.autoslice.final_review_contract import is_release_resolved_keep
    current = '阿珍' + ('没' if negated else '') + '听到了'
    comparison = '阿强' + ('没' if negated else '') + '听到了'
    srt = protocol.SRT.replace('我听到了，呜呜', current)
    monkeypatch.setattr(protocol, 'SRT', srt)
    finding = {**protocol.FINDING, 'suspect': current, 'suggestion': comparison,
        'proposed_full_cue': comparison}
    fixture = protocol.build_offline_rig(tmp_path, monkeypatch, srt=srt, finding=finding)
    def reply(data, refs):
        return {**c_reply(data, refs), 'name_only': True, 'name_span': [0, 2],
            'comparison_text': comparison, 'best_guess': '阿强', 'reason': '无来源姓名用字保留现稿，槽外原话必须保护。'}
    audit, captured = produce(fixture, reply, wide=('阿强听到了', '阿雪听到了'))
    if negated:
        # The C meaning contract can reject first; the name guard must also
        # reject this REAL producer request when exercised at its own boundary.
        assert audit.get('reason_code') in {'TERMINAL_KEEP_MEANING_CONTRACT_REQUIRED',
            'EDITORIAL_CLEAR_AUDIO_CONFLICT_OUTSIDE_NAME'}, audit
        from src.autoslice.semantic_terminal_adjudication import protect_outside_name_slot, terminal_observation_projection
        request = captured['request']
        with pytest.raises(ValueError, match='EDITORIAL_CLEAR_AUDIO_CONFLICT_OUTSIDE_NAME'):
            protect_outside_name_slot(request['check_request'], request['audio'], [0, 2],
                projection=terminal_observation_projection(request))
    else:
        assert audit.get('policy_branch') == 'SUB18_CONTEXT_MEANING_EQUIVALENT_KEEP', audit
        assert is_release_resolved_keep({**audit['rebuilt_finding'], 'exact_release_adjudication': audit})
