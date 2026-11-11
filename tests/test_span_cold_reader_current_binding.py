"""Historical ordinary writer -> native checkpoint cold-consumer regression."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace

import pytest

from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice import span_finding_obligations as spans
from src.autoslice.review_evidence_store import load_review, persist_review
from src.autoslice.span_phoneme_resolution import digest
from tests import test_span_obligation_closure as closure
from tests.test_producer_current_text_auto_resume import _checkpoint, _completed_current_review, _canonical_context_sha


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError('No provider or network calls')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)


def legacy_writer(tmp_path):
    """Run the actual historic writer, rather than inventing its saved states."""
    root = Path(__file__).resolve().parents[1]
    saved = os.environ.get('SPAN_LEGACY_WRITER')
    raw = Path(saved).read_bytes() if saved else subprocess.check_output(['git', '-C', str(root), 'show',
        '08de3cde30abd9f775afae3f641d3b36a1b09173:src/autoslice/span_finding_obligations.py'])
    assert hashlib.sha256(raw).hexdigest() == '0606114ee56cea3d131aa40fc7c5987ba39112e0f1f99060e3aba6f22a45fc51'
    path = tmp_path / 'legacy_span.py'
    path.write_bytes(raw)
    spec = importlib.util.spec_from_file_location('src.autoslice.legacy_span_writer', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def legacy_checkpoint_input(tmp_path, monkeypatch, *, cue=1):
    if cue == 2:
        from tests import test_span_phoneme_resolution as phoneme
        from tests import test_terminal_rework2_obligations as terminal
        second = ('1\n00:00:00,000 --> 00:00:01,000\n开头\n\n'
                  + closure._SRT.replace('1\n00:00:01', '2\n00:00:01', 1)
                  .replace('2\n00:00:04', '3\n00:00:04', 1)
                  .replace('3\n00:00:05', '4\n00:00:05', 1)
                  .replace('4\n00:00:07', '5\n00:00:07', 1))
        monkeypatch.setattr(phoneme, 'srt', lambda text: second.replace('你想，甲0呀', text))
        monkeypatch.setattr(terminal, 'run_case', lambda **kw:
                            phoneme.run_case(**kw, finding_extra={'cue_index':2}))
        monkeypatch.setattr(terminal, '_SRT', second)
        monkeypatch.setattr(terminal, '_FINDING', {**terminal._FINDING, 'cue_index':2})
        plan = terminal._assisted_plan(); plan.update(first_cue=2, last_cue=3, context_refs=['cue:4'])
        monkeypatch.setattr(terminal, '_assisted_plan', lambda: plan)
        monkeypatch.setattr(closure, '_SRT', second)
    authority, row = closure.case(tmp_path / 'review', monkeypatch)
    old = spans.enforce_review(closure._SRT, authority, closure.review(row))
    history = deepcopy(old['span_obligation_review']['closures'])
    authority['span_obligation_closures'] = history
    authority['final_review_audit'] = old
    changed = closure._SRT.replace('中间', '另处已修改')
    review, _ = _completed_current_review(changed)
    boundary = review['boundary_semantic_review']
    end_cue = len(pipeline.parse_srt_cues(changed))
    boundary['final_endpoint_binding'].update(final_start_ms=0, final_end_ms=9000,
        recommended_end_cue_index=end_cue, final_closure_cue_index=end_cue,
        recommended_end_ms=9000, final_snapped_end_ms=9000)
    boundary['source_separation_witness'].update(source_final_start_ms=0, source_final_end_ms=9000)
    review.update(reason_codes=[], unresolved_findings_disclosed=[], resolved_findings=[])
    legacy = legacy_writer(tmp_path).enforce_review(changed, authority, review)
    authority.update(schema_version='chat-authority-audit.v2',
        final_output_srt_sha256=hashlib.sha256(changed.encode()).hexdigest(),
        final_review_audit=legacy, entity_repairs=[])
    path = tmp_path / 'historical-authority.json'
    persist_review(path, authority)
    return path, changed, deepcopy(history)


@pytest.mark.parametrize('cue', [1, 2])
def test_saved_legacy_writer_is_pending_at_normal_continuation_consumer(tmp_path, monkeypatch, cue):
    path, changed, history = legacy_checkpoint_input(tmp_path, monkeypatch, cue=cue)
    before = path.read_bytes()
    authority = load_review(path)
    with pytest.raises(ValueError, match='SPAN_KEEP_CLOSURE_CURRENT_SRT_DRIFT'):
        spans.validate_release_obligations(authority['final_review_audit'])
    resumed = pipeline._continuation_validate_existing_authority(
        authority, srt_text=changed, srt_sha256=hashlib.sha256(changed.encode()).hexdigest(),
        clip_context={}, candidate_id='synthetic')
    assert resumed['release_gate'] == 'BLOCK' and resumed['findings']
    assert resumed['span_obligation_review']['closures'] == history
    assert any(row['status'] != 'RESOLVED' for row in resumed['span_obligation_review']['states'])
    with pytest.raises(ValueError):
        pipeline.validate_final_review_release(resumed,
            expected_srt_sha256='sha256:' + hashlib.sha256(changed.encode()).hexdigest())
    assert path.read_bytes() == before
    # The ordinary recovery writer can persist this BLOCK and restart again.
    authority['final_review_audit'] = resumed
    persist_review(tmp_path / 'pending-authority.json', authority)
    cold_again = load_review(tmp_path / 'pending-authority.json')
    again = pipeline._continuation_validate_existing_authority(
        cold_again, srt_text=changed, srt_sha256=hashlib.sha256(changed.encode()).hexdigest(),
        clip_context={}, candidate_id='synthetic')
    assert again['release_gate'] == 'BLOCK' and again['findings']
    assert again['span_obligation_historical_proof'] == resumed['span_obligation_historical_proof']


def test_same_current_closure_remains_valid_without_projection(tmp_path, monkeypatch):
    authority, row = closure.case(tmp_path, monkeypatch)
    current = spans.enforce_review(closure._SRT, authority, closure.review(row))
    review, _ = _completed_current_review(closure._SRT)
    boundary = review['boundary_semantic_review']
    boundary['final_endpoint_binding'].update(final_start_ms=0, final_end_ms=9000,
        recommended_end_cue_index=4, final_closure_cue_index=4, recommended_end_ms=9000, final_snapped_end_ms=9000)
    boundary['source_separation_witness'].update(source_final_start_ms=0, source_final_end_ms=9000)
    review.update(current)
    sha = hashlib.sha256(closure._SRT.encode()).hexdigest()
    authority.update(schema_version='chat-authority-audit.v2', final_output_srt_sha256=sha,
                     final_review_audit=review, entity_repairs=[])
    result = pipeline._continuation_validate_existing_authority(
        authority, srt_text=closure._SRT, srt_sha256=sha, clip_context={}, candidate_id='synthetic')
    assert result['release_gate'] == 'PASS'
    assert 'span_obligation_historical_proof' not in result


def _ordinary_checkpoint(tmp_path, monkeypatch, *, cue=1):
    proof_dir = tmp_path / 'historical'; proof_dir.mkdir()
    old_path, changed, history = legacy_checkpoint_input(proof_dir, monkeypatch, cue=cue)
    authority = load_review(old_path)
    checkpoint = _checkpoint(tmp_path)
    out = checkpoint['out_root']; padded = checkpoint['padded']
    checkpoint['spec']['pieces'][0]['end_ms'] = 10000
    old_piece = out / 'piece_0_0_100.mp4'
    piece = out / 'piece_0_0_10000.mp4'
    old_piece.rename(piece)
    provenance = json.loads(old_piece.with_suffix('.provenance.json').read_text())
    provenance.update(end_ms=10000, output_path=str(piece.resolve()))
    piece.with_suffix('.provenance.json').write_text(json.dumps(provenance))
    padded_provenance = json.loads(padded.with_suffix('.provenance.json').read_text())
    padded_provenance['inputs'][0]['path'] = str(piece.resolve())
    padded.with_suffix('.provenance.json').write_text(json.dumps(padded_provenance))
    context = json.loads(checkpoint['context_path'].read_text())
    context['pieces'][0]['end_ms'] = 10000
    context['whole_clip_draft_srt'] = changed
    context['whole_clip_draft_srt_sha256'] = 'sha256:' + hashlib.sha256(changed.encode()).hexdigest()
    context.pop('context_sha256'); context['context_sha256'] = _canonical_context_sha(context)
    checkpoint['context_path'].write_text(json.dumps(context))
    current = out / 'candidate.current.srt'; current.write_text(changed)
    sha = hashlib.sha256(changed.encode()).hexdigest()
    checkpoint['spec'].update(semantic_end_ms=9000, given_end_ms=9000,
        given_end_mode='exact_source_pin', human_truth_mode='withheld', boundary_repair_extend_cap_ms=30000)
    owner_audit = {}
    pipeline.freeze_required_boundary_owner_contract(spec=checkpoint['spec'], durations=[10000],
        chat_authority_audit=owner_audit, required_boundary_owners=[])
    owner = owner_audit['frozen_boundary_owner_contract']
    authority.update(post_transcript_entity_output_srt=changed,
                     post_transcript_entity_output_srt_sha256=sha,
                     frozen_boundary_owner_contract=owner)
    persist_review(checkpoint['authority_path'], authority)
    clock_review, _ = _completed_current_review(changed)
    b = clock_review['boundary_semantic_review']; b['candidate_id'] = checkpoint['cid']
    end_cue = len(pipeline.parse_srt_cues(changed))
    b['final_endpoint_binding'].update(final_start_ms=0, final_end_ms=9000,
        recommended_end_cue_index=end_cue, final_closure_cue_index=end_cue,
        recommended_end_ms=9000, final_snapped_end_ms=9000)
    b['source_separation_witness'].update(source_final_start_ms=0, source_final_end_ms=9000)
    clock = {'schema_version': 'chat-authority-audit.v2', 'final_output_srt_sha256':sha,
             'post_transcript_entity_output_srt_sha256':sha, 'frozen_boundary_owner_contract':owner,
             'entity_repairs':[], 'final_review_audit':clock_review}
    clock_path = out / 'clock-authority.json'; clock_path.write_text(json.dumps(clock))
    binding = lambda p: {'path':str(p.resolve()), 'sha256':'sha256:' + hashlib.sha256(p.read_bytes()).hexdigest()}
    from src.autoslice.producer_text_checkpoint import write_current_text_checkpoint
    manifest = out / 'candidate.current-text-checkpoint.json'
    write_current_text_checkpoint(manifest, candidate_id=checkpoint['cid'], srt_path=current,
        authority_path=checkpoint['authority_path'], context_path=checkpoint['context_path'], padded=padded,
        source_clock={'authority':binding(clock_path), 'srt':binding(current)})
    return checkpoint, manifest, changed, history


@pytest.mark.parametrize('cue', [1, 2])
def test_normal_checkpoint_writer_then_automatic_cold_reader(tmp_path, monkeypatch, cue):
    checkpoint, manifest, changed, history = _ordinary_checkpoint(tmp_path, monkeypatch, cue=cue)
    native_bytes = manifest.read_bytes()
    native = json.loads(native_bytes)
    original_authority = Path(native['authority']['path']).read_bytes()
    monkeypatch.setattr(pipeline, '_transcribe_draft', lambda **kwargs:
        SimpleNamespace(spans=[], transcriber=lambda: None))
    resumed = pipeline._automatic_current_text_checkpoint(
        spec=checkpoint['spec'], durations=[10000], padded=checkpoint['padded'], padded_dur=10000,
        host='localhost', cid=checkpoint['cid'], out_root=checkpoint['out_root'], substrate='existing_srt',
        correct='cpa', screen_text=False, adapters=None, merged=[], authoritative_chat=[])
    assert resumed is not None and resumed.current_srt_path.read_text() == changed
    snapshot = pipeline._automatic_current_checkpoint_snapshot(
        spec=checkpoint['spec'], durations=[10000], padded=checkpoint['padded'], padded_dur=10000,
        cid=checkpoint['cid'], out_root=checkpoint['out_root'])
    clock_review, clock_ref = pipeline._continuation_source_clock_reference(
        pipeline._current_checkpoint_clock_namespace(pipeline._current_checkpoint_clock_bindings(snapshot['native_checkpoint'])),
        chat_authority=snapshot['chat_authority'], srt_text=changed, srt_sha256=snapshot['srt_sha256'],
        clip_context=snapshot['clip_context'], candidate_id=checkpoint['cid'])
    projected = pipeline._continuation_validate_existing_authority(snapshot['chat_authority'], srt_text=changed,
        srt_sha256=snapshot['srt_sha256'], clip_context=snapshot['clip_context'], candidate_id=checkpoint['cid'],
        source_clock_review=clock_review, source_clock_srt_sha256=clock_ref['reviewed_srt_sha256'],
        allow_source_scope_input=True)
    assert projected['release_gate'] == 'BLOCK' and projected['findings']
    assert projected['span_obligation_review']['closures'] == history
    assert projected['span_obligation_historical_proof']['closures'] == history
    with pytest.raises(ValueError):
        pipeline.validate_final_review_release(projected, expected_srt_sha256='sha256:' + snapshot['srt_sha256'])
    assert manifest.read_bytes() == native_bytes and Path(native['authority']['path']).read_bytes() == original_authority


def test_current_pending_reaches_normal_review_gate_after_automatic_reader(tmp_path, monkeypatch):
    checkpoint, manifest, _changed, history = _ordinary_checkpoint(tmp_path, monkeypatch)
    monkeypatch.setattr(pipeline, '_transcribe_draft', lambda **kwargs:
        SimpleNamespace(spans=[], transcriber=lambda: None))
    automatic = pipeline._automatic_current_text_checkpoint(
        spec=checkpoint['spec'], durations=[10000], padded=checkpoint['padded'], padded_dur=10000,
        host='localhost', cid=checkpoint['cid'], out_root=checkpoint['out_root'], substrate='existing_srt',
        correct='cpa', screen_text=False, adapters=None, merged=[], authoritative_chat=[])
    continuation = replace(automatic, verify_confusable_entity=lambda *_a, **_kw: None)
    original = manifest.read_bytes()
    from src.autoslice import producer_package_finalization as finalizer
    class ReachedReview(Exception): pass
    def review_gate(**kwargs):
        audit = kwargs['chat_authority_audit']['final_review_audit']
        assert audit['release_gate'] == 'BLOCK' and audit['findings']
        assert audit['span_obligation_review']['closures'] == history
        assert audit['span_obligation_historical_proof']['closures'] == history
        raise ReachedReview('normal content review reached')
    monkeypatch.setattr(finalizer, '_run_exact_final_review_gate', review_gate)
    adapters = SimpleNamespace(profile_asset_file=lambda _name: None)
    with pytest.raises(ReachedReview, match='normal content review reached'):
        pipeline._run_current_text_continuation(spec=checkpoint['spec'], durations=[10000],
            padded=checkpoint['padded'], padded_dur=10000, host='localhost', cid=checkpoint['cid'],
            out_root=checkpoint['out_root'], adapters=adapters, continuation=continuation)
    assert json.loads(original)['release_authorized'] is False
    recovered = json.loads(manifest.read_bytes())
    assert recovered['authority']['sha256'] != json.loads(original)['authority']['sha256']
    saved = load_review(Path(recovered['authority']['path']))['final_review_audit']
    assert saved['release_gate'] == 'BLOCK' and saved['findings']
    assert saved['span_obligation_historical_proof']['closures'] == history
    restarted = pipeline._automatic_current_text_checkpoint(
        spec=checkpoint['spec'], durations=[10000], padded=checkpoint['padded'], padded_dur=10000,
        host='localhost', cid=checkpoint['cid'], out_root=checkpoint['out_root'], substrate='existing_srt',
        correct='cpa', screen_text=False, adapters=None, merged=[], authoritative_chat=[])
    assert restarted is not None


@pytest.mark.parametrize('damage', ['source', 'clock', 'srt_hash', 'context', 'padded'])
def test_checkpoint_bindings_are_not_repaired_by_span_recovery(tmp_path, monkeypatch, damage):
    checkpoint, manifest, _changed, _history = _ordinary_checkpoint(tmp_path, monkeypatch)
    native = json.loads(manifest.read_bytes())
    if damage == 'source':
        checkpoint['spec']['pieces'][0]['source_media_sha256'] = 'sha256:' + 'f' * 64
    elif damage == 'clock':
        native['source_clock']['srt']['sha256'] = 'sha256:' + 'f' * 64
    elif damage == 'srt_hash':
        native['srt']['sha256'] = 'sha256:' + 'f' * 64
    elif damage == 'context':
        native['context']['sha256'] = 'sha256:' + 'f' * 64
    else:
        native['padded']['sha256'] = 'sha256:' + 'f' * 64
    if damage != 'source': manifest.write_text(json.dumps(native))
    monkeypatch.setattr(pipeline, '_transcribe_draft', lambda **kwargs: pytest.fail('bad binding reached media stage'))
    with pytest.raises(pipeline.CurrentTextContinuationError):
        pipeline._automatic_current_text_checkpoint(
            spec=checkpoint['spec'], durations=[10000], padded=checkpoint['padded'], padded_dur=10000,
            host='localhost', cid=checkpoint['cid'], out_root=checkpoint['out_root'], substrate='existing_srt',
            correct='cpa', screen_text=False, adapters=None, merged=[], authoritative_chat=[])


@pytest.mark.parametrize('damage', ['wrong_srt', 'proof_hash', 'wrong_clock', 'wrong_window',
                                   'missing_risk', 'missing_no_gain', 'meaning_reversal', 'unknown'])
def test_historical_integrity_failure_cannot_become_a_current_pending_input(tmp_path, monkeypatch, damage):
    path, changed, _ = legacy_checkpoint_input(tmp_path, monkeypatch)
    authority = load_review(path)
    proof = authority['final_review_audit']['span_obligation_review']
    if damage == 'wrong_srt':
        changed += '\n'
    elif damage == 'proof_hash':
        proof['receipt_sha256'] = '0' * 64
    else:
        prior = deepcopy(proof['closures'][0])
        if damage == 'wrong_clock':
            prior['adjudication']['request']['source_media_timeline_offset_ms'] += 1
        elif damage == 'wrong_window':
            prior['adjudication']['request']['matched_start_ms'] += 1
        elif damage == 'missing_risk':
            prior['finding'].pop('current_content_risk')
        elif damage == 'missing_no_gain':
            prior['adjudication']['sub18_context_meaning_equivalent_keep'].pop('no_gain_proof')
        elif damage == 'meaning_reversal':
            prior['finding']['content_risk'] = 'meaning_reversal'
        else:
            prior['finding']['current_content_risk']['status'] = 'UNKNOWN'
        proof['closures'][0] = prior
        authority['span_obligation_closures'][0] = deepcopy(prior)
        proof['receipt_sha256'] = digest({k:v for k,v in proof.items() if k != 'receipt_sha256'})
    with pytest.raises((pipeline.CurrentTextContinuationError, ValueError)):
        pipeline._continuation_validate_existing_authority(authority, srt_text=changed,
            srt_sha256=hashlib.sha256(changed.encode()).hexdigest(), clip_context={}, candidate_id='synthetic')
