"""Terminal source-owner requests retain their real stopping proof at admission."""

import copy
import hashlib
import html
import json
import wave

import pytest

from src.autoslice import chat_authority_reconciliation as reconcile
from src.autoslice import semantic_terminal_adjudication as terminal
from src.autoslice.chat_evidence import ChatEvidence
from src.autoslice.final_review_auditor import _source_owner_budget_adapter
from src.autoslice.final_review_provider_budget import ContextAdjudicationBudget
from tests.test_chat_authority_conflict_handoff import _chat_authority
from tests.test_exact_final_funnel import (
    SRT, answer, run_terminal, terminal_rig, unresolved,  # noqa: F401
)


def _retained_terminal_inputs(rig):
    # Produce a real retained stopping proof with the existing offline audio
    # fixture. Only provider answers are synthetic; proof/source validation runs.
    result = run_terminal([unresolved()], lambda _: json.dumps(answer(), ensure_ascii=False),
                          terminal_rig=rig)
    finding, = result['unresolved_findings_disclosed']
    request = finding['exact_release_adjudication']['terminal_review']['request']
    return finding, request


def _owner_inputs(tmp_path, rig):
    # Source-owner replay needs an explicit recording clock. Keep the fixture's
    # four-second media unchanged, and bind it to a real five-second recording
    # whose first second is silence and whose remaining PCM is exactly the clip.
    # Write the normal producer sidecars before any observation/proof is made.
    from src.autoslice.clip_context import build_clip_context
    from src.autoslice.final_review_auditor import build_context_adjudication_request
    from src.autoslice.independent_context_adjudication import _range_media_source_binding
    with wave.open(str(rig.source), 'rb') as stream:
        parameters = stream.getparams()
        frames = stream.readframes(parameters.nframes)
    recording = tmp_path / 'original-recording.wav'
    with wave.open(str(recording), 'wb') as stream:
        stream.setparams(parameters)
        stream.writeframes(bytes(parameters.framerate * parameters.nchannels * parameters.sampwidth) + frames)
    recording_sha = hashlib.sha256(recording.read_bytes()).hexdigest()
    media_sha = hashlib.sha256(rig.source.read_bytes()).hexdigest()
    piece = tmp_path / 'recording-piece.wav'
    piece.write_bytes(rig.source.read_bytes())
    end_ms = 1000 + parameters.nframes * 1000 // parameters.framerate
    piece.with_suffix('.provenance.json').write_text(json.dumps({
        'source_path': str(recording), 'source_sha256': recording_sha,
        'source_media_binding': 'sha256:' + recording_sha,
        'start_ms': 1000, 'end_ms': end_ms,
        'output_path': str(piece), 'output_sha256': media_sha}))
    rig.source.with_suffix('.provenance.json').write_text(json.dumps({
        'inputs': [{'path': str(piece), 'sha256': media_sha}],
        'output_path': str(rig.source), 'output_sha256': media_sha}))
    rig.context = build_clip_context(
        candidate_id='synthetic-source-owner-case',
        spec={'date': '2026-10-19', 'pieces': [{'start_ms': 1000, 'end_ms': end_ms,
              'source_media_sha256': 'sha256:' + recording_sha}]},
        draft_srt=SRT, authoritative_chat=(), topic_resolution={}, session_topic_authorities=(),
        speech_memory_ledger_path=rig.tmp / 'empty-speech-memory.json', source_truth_ledger_path=None)
    rig.check = build_context_adjudication_request(
        SRT, rig.finding, clip_context=rig.context, source_media_timeline_offset_ms=rig.offset_ms)
    binding = _range_media_source_binding(rig.check, str(rig.source), media_sha)
    assert binding['role'] == 'PRODUCER_PADDED_MEDIA_FROM_RECORDING'
    assert binding['recording_start_ms'] == 1000

    finding, request = _retained_terminal_inputs(rig)
    proof = request['audio_gain_termination']
    origins = {row['media_source_projection']['media_source_binding']['recording_start_ms']
               for row in proof['projection']['observations']
               if row.get('media_source_projection', {}).get('media_source_binding')}
    assert len(origins) == 1
    original_ms = origins.pop() + 42
    current = request['current_cue']
    xml = tmp_path / 'source-owner.xml'
    xml.write_text(f'<i><d p="{original_ms // 1000}.{original_ms % 1000:03d},1">'
                   f'{html.escape(current)}</d></i>', encoding='utf-8')
    authority = _chat_authority()
    authority['applied'][0].update(
        source=str(xml), source_sha256=hashlib.sha256(xml.read_bytes()).hexdigest(),
        evidence_id=ChatEvidence('danmaku', 42, current).evidence_id,
        matched_start_ms=rig.offset_ms + 1000, matched_end_ms=rig.offset_ms + 2000,
        exact_text=current, before=['我喜欢她呀'], after=[current])
    owner_request = reconcile.build_owner_request(
        SRT, finding, authority, delivery_start_ms=rig.offset_ms)
    reconcile.validate_owner_request(owner_request)
    assert owner_request['audio_gain_termination'] == proof
    return finding, owner_request


def _budget(rig, call):
    return ContextAdjudicationBudget(
        limit=5, entity_verifier=None, clip_context=rig.context,
        source_media_timeline_offset_ms=rig.offset_ms, judge_llm_call=call,
        screen_read_probe=None, adjudicate_context=lambda *_args, **_kwargs: None)


def _transport(tmp_path, calls):
    def call(prompt):
        calls.append(prompt)
        return json.dumps(answer(), ensure_ascii=False)
    call.cpa_cache_identity = {
        'transport': 'recorded', 'models': ['source-owner-fixture'], 'effort': 'medium'}
    call.completed_review_root = tmp_path / 'completed-owner'
    return call


def test_source_owner_budget_binds_original_stop_proof_and_cold_replays(
        tmp_path, monkeypatch, terminal_rig):  # noqa: F811
    finding, request = _owner_inputs(tmp_path, terminal_rig)
    before = copy.deepcopy(request)
    runtime = tmp_path / 'runtime-owner'
    monkeypatch.setenv('AUTOSLICE_BASE', str(runtime))
    calls = []
    call = _transport(tmp_path, calls)
    budget = _budget(terminal_rig, call)
    result = _source_owner_budget_adapter(budget)(SRT, finding, request)

    assert result['status'] == 'VALIDATED'
    assert result['choice'] == 'CURRENT'
    assert result['checkpoint_hit'] is False
    assert budget.provider_adjudication_count == 1
    assert len(calls) == 1 and calls[0].startswith(terminal.MARKER)
    assert '原弹幕来源的裁决者' in calls[0]
    rows = list((runtime / 'cache/semantic-cpa-attempts').rglob('*.json'))
    assert len(rows) == 1
    recorded = json.loads(rows[0].read_text())
    assert recorded['status'] == 'COMPLETED'
    assert recorded['request']['terminal_no_gain_proof_sha256'] == request[
        'audio_gain_termination']['proof_sha256']
    original_bytes = rows[0].read_bytes()

    # A new budget instance must reopen the same saved response with zero
    # additional dispatches, without rewriting the original attempt/proof.
    cold = _budget(terminal_rig, call)
    replay = _source_owner_budget_adapter(cold)(SRT, finding, request)
    assert replay['status'] == 'VALIDATED' and replay['checkpoint_hit'] is True
    assert cold.provider_adjudication_count == 0 and len(calls) == 1
    assert rows[0].read_bytes() == original_bytes
    assert request == before


@pytest.mark.parametrize('damage', ['missing', 'changed_raw'])
def test_common_terminal_admission_rejects_invalid_stop_proof_without_dispatch(
        tmp_path, monkeypatch, terminal_rig, damage):  # noqa: F811
    finding, request = _retained_terminal_inputs(terminal_rig)
    prompt = terminal.terminal_prompt(request)
    broken = copy.deepcopy(request)
    if damage == 'missing':
        broken.pop('audio_gain_termination')
    else:
        broken['audio_gain_termination']['raw_completion'] += ' '
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path / 'runtime-rejected'))
    calls = []
    budget = _budget(terminal_rig, _transport(tmp_path, calls))

    def adjudicate(text, _finding, **kwargs):
        terminal.request_terminal_answer(kwargs['judge_llm_call'], prompt, broken)
        raise AssertionError('invalid stop proof reached a terminal answer')

    with pytest.raises(ValueError, match='RELISTEN_NO_GAIN_(PROOF_REQUIRED|BINDING_DRIFT)'):
        budget.prepare(SRT, finding, adjudicate_context=adjudicate)()
    assert calls == []
    assert not list((tmp_path / 'runtime-rejected/cache/semantic-cpa-attempts').rglob('*.json'))
