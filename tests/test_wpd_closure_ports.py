"""Port contract tests using a recorded producer substitute, never a provider.

The shared C modules are required in the integration snapshot. A D-only tree
explicitly skips this file; that skip is not production or port acceptance.
"""
from dataclasses import replace
import json

import pytest

from src.autoslice import discovery_channels as channels
from src.autoslice.finding_context import materials_for_check, question_key
from tests.test_exact_final_funnel import terminal_rig  # noqa: F401


TEXT = '1\n00:00:03,000 --> 00:00:05,000\n一句\n\n2\n00:00:06,000 --> 00:00:08,000\n二句\n'


def test_c_and_d_fullclip_prefetch_share_existing_wide_receipt_identity():
    ports = pytest.importorskip('src.autoslice.finding_closure_ports', reason='requires the C+D integration snapshot')
    from src.autoslice.finding_closure_shell import ExistingFindingOwners, OwnerInputs
    from src.autoslice.context_assisted_audio import digest
    from src.autoslice.targeted_relisten import physical_target
    from src.autoslice.native_context_witness import _prepare_request
    source = channels.SourceIdentity('clip', ('a' * 64,))
    context = {'candidate_id': 'clip', 'pieces': [{'source_media_sha256': 'a' * 64}]}
    for offset in (0, 10000):
        check, wide = channels.fullclip_prefetch_geometry(TEXT, source=source, source_offset_ms=offset)
        assert check['require_complete_utterance_support'] is True
        assert check['candidate_provenance']['mutation_authorized'] is False
        assert wide['evidence_id'] == digest({'physical_target': physical_target(check),
            'channel': 'WIDE_CANDIDATE_BLIND_AUDIO'})
        assert wide['request_sha256'] == digest({k: v for k, v in wide.items() if k != 'request_sha256'})
        assert _prepare_request(wide).request_sha256 == wide['request_sha256']
        assert 'syllable_count_hint' not in wide and 'current_cue' not in wide
        calls = []
        def observe(**kwargs):
            calls.append(kwargs)
            return []  # Transparent capture; no provider or audio acquisition.
        observe.available_providers = ('mai', 'moss')
        verifier = type('RecordedVerifier', (), {})()
        verifier.observe_range_context = observe
        assert channels.prefetch_fullclip(TEXT, verifier=verifier, source=source, source_offset_ms=offset) == ()
        assert {call['provider'] for call in calls} == {'mai', 'moss'}
        assert all(call['check_request'] == check and call['range_witness_request'] == wide for call in calls)
        owners = ExistingFindingOwners(OwnerInputs('a' * 64, offset,
            ports.RetainedObject.from_value(context), ports.RetainedObject.from_value({}),
            ports.RetainedObject.from_value({})))
        c_check, c_wide = owners._prefetch_geometry(TEXT)
        assert c_check.value() == check
        assert c_wide.value()['evidence_id'] == wide['evidence_id']
        assert c_wide.value()['request_sha256'] == wide['request_sha256']


@pytest.fixture
def setup_ports(monkeypatch):
    ports = pytest.importorskip('src.autoslice.finding_closure_ports', reason='requires the C+D integration snapshot')
    from src.autoslice import finding_closure as core
    from src.autoslice import producer_text_pipeline as pipeline
    from src.autoslice import microcue_acoustic_discovery as microcue
    calls = []
    context = {'candidate_id': 'clip', 'pieces': [{'source_media_sha256': 'a' * 64}]}

    def prepare(text, original):
        row = original.value()
        index = row.get('cue_index', row.get('cue'))
        cue = channels.cue_versions(text)[index - 1]
        check = {'matched_start_ms': cue.start_ms, 'matched_end_ms': cue.end_ms,
            'source_media_timeline_offset_ms': 0, 'current_cue': '一句' if index == 1 else '二句',
            'cue_indexes': [index], 'whole_clip_current_srt': text,
            'clip_context_binding': {'candidate_id': 'clip', 'source_media_sha256s': ['a' * 64]}}
        candidates = [r['proposed_full_cue'] for r in row['discovery_candidates']
            if isinstance(r.get('proposed_full_cue'), str)]
        material = materials_for_check(check, candidates=[check['current_cue'], *candidates],
            observation_ids=tuple(row['discovery_task']['bound_observation_ids']),
            contracts={'test': 'recorded-port-producer'}, model_identity={'stage': 'fixture'})
        retained_check = ports.RetainedObject.from_value(check)
        retained_material = ports.RetainedObject.from_value(material)
        spec = core.TaskSpec(core.FindingKey(question_key(check), material['materials_sha256']),
            (index,), core.local_text_sha(text, (index,)), original.sha256,
            retained_check.sha256, retained_material.sha256)
        return ports.PreparedFinding(spec, original, retained_check, retained_material)

    def recorded_model(prompt):
        calls.append(prompt)
        return json.dumps({'findings': [
            {'cue': i, 'suspect': '一句' if i == 1 else '二句', 'proposed_full_cue': None,
             'kind': 'context', 'repair_class': 'phonetic', 'why': '录答候选'} for i in (1, 2)]})

    def recorded_pipeline(*, srt_text, final_review_llm, discovery_result_sink, scope_cues, **kwargs):
        raw = json.loads(final_review_llm('独立发现\n' + srt_text))['findings']
        result = channels.union_discovery(srt_text, channels.DiscoveryBatch(
            channels.nominations_from_rows(raw, channel=channels.Channel.TEXT),
            channels.SourceIdentity('clip', ('a' * 64,))), scope_cues=scope_cues)
        discovery_result_sink(result)
        return {'status': 'DISCOVERY_COMPLETE', 'scan_coverage_complete': True,
            'findings': channels.task_rows(result)}

    def verifier(request):
        pytest.fail('this recorded producer does not request narrow audio')
    verifier.span_termination_source_identity = lambda: 'a' * 64
    monkeypatch.setattr(microcue, 'discover_microcue_findings', lambda *a, **k: ([], {
        'status': 'PASS', 'srt_sha256': 'sha256:' + core.sha(a[0])}))
    monkeypatch.setattr(channels, 'prefetch_fullclip', lambda *a, **k: (
        {'provider': 'mai', 'status': 'OBSERVED'}, {'provider': 'moss', 'status': 'OBSERVED'}))
    monkeypatch.setattr(pipeline, '_run_exact_final_release_review', recorded_pipeline)
    inputs = ports.DiscoveryInputs('a' * 64, 0, '', '', '',
        ports.RetainedObject.from_value(context), ports.RetainedObject.from_value({
            'audio_language_discovery_audit': {'scan': {'status': 'OBSERVED'}}}),
        ports.RetainedObject.from_value({}), (), (), prepare, recorded_model, json.loads,
        entity_verifier=verifier, candidate_id='clip')
    return ports, core, inputs, calls


def work(core, kind, scope=(1, 2), closed=()):
    return core.Work('work-' + kind, kind, core.sha(TEXT), scope_cues=scope,
        closed_ledger=closed, scan_channels=channels.scan_channels())


def test_three_scan_ports_share_channels_scope_and_canonical_closed_filter(setup_ports):
    ports, core, inputs, calls = setup_ports
    discovery = channels.build_closure_discovery_ports(inputs)
    first = discovery.first_scan(ports.ScanRequest(work(core, 'initial_scan'), TEXT))
    assert len(first.findings) == 2
    assert len(calls) == channels.DEFAULT_TEXT_LANES
    assert tuple(lane.value()['channel'] for lane in first.lanes) == channels.scan_channels()
    target = discovery.targeted_scan(ports.ScanRequest(work(core, 'targeted_scan', (2,)), TEXT))
    assert len(target.findings) == 1 and target.coverage == (core.cue_versions(TEXT)[1],)
    closed = (core.ClosedFinding(first.findings[0].spec.key, 'b' * 64),)
    final = discovery.final_scan(ports.ScanRequest(work(core, 'final_scan', closed=closed), TEXT))
    assert len(final.findings) == 1
    assert final.findings[0].spec.key != closed[0].key
    assert tuple(lane.value()['channel'] for lane in final.lanes) == channels.scan_channels()
    assert all('closed_ledger' not in prompt for prompt in calls)


def test_scan_cold_replays_original_calls_without_a_live_fallback(setup_ports):
    ports, core, inputs, calls = setup_ports
    discovery = channels.build_closure_discovery_ports(inputs)
    item = work(core, 'initial_scan')
    scan = discovery.first_scan(ports.ScanRequest(item, TEXT))
    event = core.ScanCompleted(item.work_id, tuple(p.spec for p in scan.findings), scan.coverage,
        tuple(lane.sha256 for lane in scan.lanes))
    objects = (*scan.lanes, *(doc for p in scan.findings for doc in (p.finding, p.check, p.materials)))
    state = core.initial_state(source_sha256='a' * 64, srt_text=TEXT, scan_channels=channels.scan_channels())
    def forbidden(prompt):
        pytest.fail('cold scan requested a live model')
    cold = channels.build_closure_discovery_ports(replace(inputs, llm_call=forbidden, entity_verifier=None))
    cold.validate_scan(ports.ValidationRequest(state, item, event, ports.ArtifactSet(objects)))
    assert len(calls) == 1
    with pytest.raises(ValueError, match='DISCOVERY_SCAN_EVENT_INVALID'):
        cold.validate_scan(ports.ValidationRequest(state, item, replace(event, work_id='another-work'),
            ports.ArtifactSet(objects)))
    tampered = scan.lanes[-1].value()
    tampered['coverage'] = 'FAILED'
    changed = ports.RetainedObject.from_value(tampered)
    bad_event = replace(event, lane_refs=(*event.lane_refs[:-1], changed.sha256))
    with pytest.raises(ValueError, match='DISCOVERY_SCAN_COLD_REPLAY_DRIFT'):
        cold.validate_scan(ports.ValidationRequest(state, item, bad_event, ports.ArtifactSet((*objects, changed))))
    assert len(calls) == 1


def test_prepare_callback_cannot_drop_discovery_observation_material(setup_ports):
    ports, core, inputs, calls = setup_ports
    def incomplete(text, original):
        prepared = inputs.prepare_finding(text, original)
        material = prepared.materials.value()
        material['bound_observation_ids'] = []
        changed = ports.RetainedObject.from_value(material)
        return replace(prepared, materials=changed, spec=replace(prepared.spec, materials_ref=changed.sha256))
    discovery = channels.build_closure_discovery_ports(replace(inputs, prepare_finding=incomplete))
    with pytest.raises(ValueError, match='DISCOVERY_PREPARE_CANDIDATES_OR_OBSERVATIONS_MISSING'):
        discovery.first_scan(ports.ScanRequest(work(core, 'initial_scan'), TEXT))
    assert len(calls) == 1


def test_asr_terminal_cannot_use_a_summary_as_original_exhaustion(setup_ports):
    ports, core, inputs, calls = setup_ports
    discovery = channels.build_closure_discovery_ports(inputs)
    scan = discovery.first_scan(ports.ScanRequest(work(core, 'initial_scan'), TEXT))
    prepared = scan.findings[0]
    row = prepared.finding.value()
    for n in row['discovery_nominations']:
        n.update(channel='C0', provider='mai', model='mai', reading='candidate')
    row['discovery_task'].update(channels=['C0'], asr_only=True)
    row['exact_release_adjudication'] = {'reason_code': 'TERMINAL_UNRESOLVABLE',
        'summary': 'all routes exhausted', 'context_exhausted': True}
    original = ports.RetainedObject.from_value(row)
    spec = replace(prepared.spec, finding_ref=original.sha256)
    item = core.Work('terminal', 'task_stop', core.sha(TEXT), task=spec,
        scan_channels=channels.scan_channels())
    event = core.ReviewCompleted(item.work_id, spec, core.State.TRUE_BLOCK, original.sha256)
    state = core.initial_state(source_sha256='a' * 64, srt_text=TEXT,
        scan_channels=channels.scan_channels())
    with pytest.raises(ValueError, match='DISCOVERY_ASR_TERMINAL_ORIGINAL_REQUIRED'):
        discovery.validate_terminal(ports.ValidationRequest(state, item, event,
            ports.ArtifactSet((original,))))
    assert len(calls) == 1


@pytest.mark.parametrize('asr_only', [False, True])
def test_terminal_port_reopens_real_f2_answer_before_asr_exhaustion_gate(
    setup_ports, terminal_rig, monkeypatch, asr_only,
):
    """Real offline F2 producer/consumer; its D alone cannot satisfy R5."""
    from tests.test_exact_final_funnel import run_terminal, unresolved, answer, SRT
    from src.autoslice.finding_lifecycle import true_block_evidence
    ports, core, inputs, calls = setup_ports
    monkeypatch.delenv('AUTOSLICE_BASE', raising=False)
    row = {**unresolved(), 'suspect': '喜欢她啊', 'suggestion': '不喜欢她',
        'proposed_full_cue': '我不喜欢她'}
    result = run_terminal([row], lambda p: json.dumps(answer('UNRESOLVABLE'), ensure_ascii=False),
        terminal_rig=terminal_rig)
    current = result['findings'][0]
    adjudication = current['exact_release_adjudication']
    assert current['lifecycle']['state'] == 'TRUE_BLOCK'
    assert true_block_evidence(adjudication)['written_meaning_error']
    nominations = channels.nominations_from_rows([row], channel=channels.Channel.TEXT)
    origins = [{'channel': 'C0' if asr_only else str(n.channel), 'cue_index': n.cue_index,
        'payload_json': n.payload_json, 'observation_id': n.observation_id,
        'provider': 'mai', 'model': 'mai', 'reading': '我不喜欢她',
        'origin_channels': []} for n in nominations]
    current['discovery_nominations'] = origins
    current['discovery_task'] = {'asr_only': asr_only, 'channels': ['C0' if asr_only else 'A']}
    retained = ports.RetainedObject.from_value(current)
    check = adjudication['terminal_review']['request']['check_request']
    material = materials_for_check(check, finding=current,
        contracts={'test': 'real-f2-original-consumer'}, model_identity={'stage': 'fixture'})
    spec = core.TaskSpec(core.FindingKey(question_key(check), material['materials_sha256']),
        (1,), core.local_text_sha(SRT, (1,)), retained.sha256, 'c' * 64, 'd' * 64)
    item = core.Work('real-terminal', 'task_stop', core.sha(SRT), task=spec,
        scan_channels=channels.scan_channels())
    event = core.ReviewCompleted(item.work_id, spec, core.State.TRUE_BLOCK, retained.sha256)
    identity = terminal_rig.local.source_sha256
    state = core.initial_state(source_sha256=identity, srt_text=SRT,
        scan_channels=channels.scan_channels())
    discovery = channels.build_closure_discovery_ports(replace(inputs, source_sha256=identity))
    request = ports.ValidationRequest(state, item, event, ports.ArtifactSet((retained,)))
    count = len(terminal_rig.transport.calls)
    if asr_only:
        with pytest.raises(ValueError, match='ASR_ONLY_CANNOT_SUSTAIN_UNRESOLVABLE'):
            discovery.validate_terminal(request)
    else:
        discovery.validate_terminal(request)
    assert len(terminal_rig.transport.calls) == count
    assert calls == []


def test_completed_postburn_delta_reopens_only_bound_p_and_cold_checks_originals(tmp_path,monkeypatch):
    from hashlib import sha256
    from dataclasses import asdict
    from src.autoslice import finding_closure as core, finding_closure_shell as shell
    from src.autoslice.finding_closure_ports import DiscoveryInputs,RetainedObject,ValidationRequest,ArtifactSet
    from src.autoslice import producer_package_finalization as producer
    from tests.test_final_subtitle_audio_gate import _postburn_record,_postburn_chat_audit,_postburn_adapters
    record,subtitle,media=_postburn_record(tmp_path)
    source=sha256(media.read_bytes()).hexdigest()
    from src.autoslice.clip_context import build_clip_context
    from src.autoslice.speech_memory_ledger import SCHEMA_VERSION
    memory=tmp_path/'speech-memory.json';memory.write_text(json.dumps({'schema_version':SCHEMA_VERSION,'entries':[]}))
    context=build_clip_context(candidate_id='candidate',
        spec={'date':'2026-10-16','pieces':[{'start_ms':0,'end_ms':40000,'source_media_sha256':'sha256:'+source}]},
        draft_srt=subtitle.read_text(),authoritative_chat=(),topic_resolution={},session_topic_authorities=(),
        speech_memory_ledger_path=memory,source_truth_ledger_path=None)
    authority=_postburn_chat_audit(subtitle);calls=[]
    _postburn_adapters(monkeypatch,json.dumps({'findings':[{'cue':4,'kind':'context','repair_class':'phonetic',
        'proposed_full_cue':'这里有更正需要审看','source_surface':'更正','why':'recorded postburn disagreement',
        'content_risk':'meaning_reversal','risk_basis':'Synthetic change to the described text.'}]}),calls)
    with pytest.raises(SystemExit,match='POSTBURN_SUBTITLE_AUDIO_REVIEW_BLOCKED'):
        producer._run_postburn_subtitle_audio_review(cid='candidate',out_root=tmp_path,
            recut=producer.FinalRecutArtifacts(tmp_path,media,subtitle,None,None),record=record,
            chat_authority_audit=authority,chat_authority_path=tmp_path/'authority.json',clip_context=context)
    assert len(calls)==1
    text=subtitle.read_text();empty=RetainedObject.from_value({});ctx=RetainedObject.from_value(context)
    from src.autoslice.final_review_meaning_policy import POLICY_VERSION
    owners=shell.ExistingFindingOwners(shell.OwnerInputs(source,0,ctx,RetainedObject.from_value({'meaning_policy':POLICY_VERSION}),
        RetainedObject.from_value({'transport':'recorded','models':['recorded'],'effort':'medium'})))
    inputs=DiscoveryInputs(source,0,'','','',ctx,empty,empty,(),(),owners.prepare_finding,
        lambda _:pytest.fail('delta must never dispatch a model'),json.loads,
        output_root=str(tmp_path),candidate_id='candidate')
    latest=replace(inputs,verified_authority=RetainedObject.from_value(authority))
    # An independent A previously opened the same ordinary upstream row.
    rows=authority['postburn_subtitle_audio_review']['findings']
    result=channels.union_discovery(text,channels.DiscoveryBatch(
        channels.nominations_from_rows(rows,channel=channels.Channel.TEXT),
        channels.source_identity(context),0))
    old,=channels._prepared_scan_findings(inputs,text,result,channels.task_rows(result),frozenset())
    initial=core.initial_state(source_sha256=source,srt_text=text,scan_channels=channels.scan_channels())
    ledger=shell.FileLedger(tmp_path/'candidate.finding-closure',initial)
    for doc in shell.finding_documents(old):ledger.put(doc)
    state=replace(initial,started=True,prefetch_finished=True,tasks=(core.Task(old.spec,core.State.KEEP,'b'*64),),
        confirmation_ref='c'*64)
    update=channels.completed_postburn_delta(latest,text,previous=inputs,previous_ref='',state=state)
    assert update is not None and len(update.findings)==1
    new=update.findings[0]
    assert new.spec.key.physical==old.spec.key.physical and new.spec.key.materials!=old.spec.key.materials
    event=core.InputsAdvanced(core.sha('discovery-update:'+update.receipt.sha256),update.receipt.sha256,(new.spec,))
    documents=(update.receipt,*shell.finding_documents(new))
    request=ValidationRequest(state,shell.control_work(state,event),event,ArtifactSet(documents))
    channels.validate_input_update(request,channels.build_closure_discovery_ports(inputs))
    following,_=core.step(state,event)
    assert not following.confirmation_ref and following.tasks[0].state==core.State.AWAITING_EVIDENCE
    assert following.discovery_ref==update.receipt.sha256 and following.current_srt==text
    assert channels.completed_postburn_delta(latest,text,previous=latest,previous_ref=following.discovery_ref,
        state=following) is None and len(calls)==1
    # Tampering with a completed original never buys another call or a pass.
    from pathlib import Path
    original=authority['postburn_subtitle_audio_review']['cpa_calls'][0]['response_path']
    Path(original).write_text('changed original')
    with pytest.raises(ValueError,match='DISCOVERY_COMPLETED_POSTBURN_BINDING_INVALID'):
        channels.validate_input_update(request,channels.build_closure_discovery_ports(inputs))
    assert len(calls)==1 and subtitle.read_text()==text
