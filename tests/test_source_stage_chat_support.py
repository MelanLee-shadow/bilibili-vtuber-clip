"""Independent chat witnesses survive media placement and checkpoint restarts."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import producer_text_checkpoint as checkpoint
from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice.chat_evidence import ChatEvidence
from src.autoslice.cue_split_hygiene import merge_release_grade_cues
from tests.test_historical_cue_window import PREFIX
from tests.test_producer_text_pipeline_final_review import _adapters
from tests.test_producer_current_text_auto_resume import (
    _checkpoint, _make_source_stage_checkpoint, _ordinary_chat_read_fixture,
)


def _case(root, kind, *, support_newline='\n'):
    root.mkdir(parents=True)
    source = PREFIX + '3\n00:00:05,000 --> 00:00:09,000\n因为李豆沙是直女\n'
    support = source.replace('直女', '侄女').replace('\n', support_newline)
    padded = root / 'padded.mp4'
    padded.write_bytes(b'bound-media')
    padded.with_suffix('.asr_draft.srt').write_text(support)
    chat = [ChatEvidence(kind, 1000, '因为李豆沙是侄女', source_sha256='a' * 64)]
    result = pipeline._apply_entity_authority(
        srt_text=source, authoritative_chat=chat,
        support_srts=(pipeline._load_independent_chat_support_srts(padded)
                      if support_newline == '\n' else [support]),
        referent_groups=[], verify_confusable_entity=lambda _: pytest.fail('provider forbidden'),
        code_switch_audit={}, term_boundary_moves=[], padded=padded, adapters=_adapters())
    text, merges = merge_release_grade_cues(result.srt_text)
    assert merges and result.chat_authority_audit['applied']
    authority = deepcopy(result.chat_authority_audit)
    checkpoint.retain_post_transcript_text(authority, text)
    authority['final_output_srt_sha256'] = hashlib.sha256(text.encode()).hexdigest()
    context = root / 'context.json'
    context.write_text(json.dumps({'whole_clip_draft_srt': source}))
    (root / 'authority.json').write_text(json.dumps(authority, ensure_ascii=False))
    (root / 'current.srt').write_text(text)
    return padded, support, text, chat, authority, context


def _write(root, lane, case, padded, **kwargs):
    _, support, text, _, authority, context = case
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'candidate.checkpoint.json'
    if lane == 'source':
        checkpoint.write_source_text_checkpoint(path, candidate_id='candidate',
            srt_path=context.parent / 'current.srt', authority_path=context.parent / 'authority.json',
            context_path=context, padded=padded, **kwargs)
    else:
        checkpoint.write_partial_source_text_checkpoint(path, candidate_id='candidate',
            srt_text=text, authority=authority, final_review={'status': 'APPLIED'},
            context_path=context, padded=padded, source_language_witness_srt=support,
            protected_name_source_srt=text, **kwargs)
    return path, json.loads(path.read_text())


def _consume(case, support):
    _, _, text, chat, authority, context = case
    pipeline._validate_source_stage_chat_reads(authority['applied'], srt_text=text,
        clip_context=json.loads(context.read_text()), authoritative_chat=chat,
        independent_support_srts=support, expected_input_srt_sha256=authority['input_srt_sha256'])


@pytest.mark.parametrize('kind', ['danmaku', 'superchat'])
@pytest.mark.parametrize('lane', ['source', 'partial'])
@pytest.mark.parametrize('legacy', [False, True])
def test_writer_restart_and_successor_preserve_actual_consumer_inputs(tmp_path, kind, lane, legacy):
    case = _case(tmp_path / 'original', kind)
    padded, support, *_ = case
    pointer, native = _write(tmp_path / 'first', lane, case, padded)
    before = pointer.read_bytes()
    if legacy:
        del native['independent_chat_support_srts']
    moved = tmp_path / 'second' / 'padded.mp4'
    moved.parent.mkdir()
    moved.write_bytes(padded.read_bytes())
    old_support = pipeline._load_independent_chat_support_srts(moved)
    assert old_support == []
    with pytest.raises(pipeline.CurrentTextContinuationError, match='CURRENT_SOURCE_STAGE_APPLIED_SUPPORT_DRIFT'):
        _consume(case, old_support)
    restored = pipeline._source_stage_chat_support_srts({'native_checkpoint': native}, padded=moved)
    assert restored == [support]
    _consume(case, restored)
    _, successor = _write(tmp_path / 'second', lane, case, moved, independent_chat_support_srts=restored)
    padded.with_suffix('.asr_draft.srt').unlink()
    _consume(case, pipeline._source_stage_chat_support_srts({'native_checkpoint': successor}, padded=moved))
    assert pointer.read_bytes() == before
    assert successor['release_authorized'] is False


@pytest.mark.parametrize('damage', ['media', 'frozen_bytes', 'malformed', 'explicit_empty'])
def test_bound_support_never_falls_back_after_corruption_or_explicit_absence(tmp_path, damage):
    case = _case(tmp_path / 'original', 'danmaku')
    padded = case[0]
    _, native = _write(tmp_path / 'checkpoint', 'source', case, padded)
    if damage == 'media':
        padded.write_bytes(b'other-media')
    elif damage == 'frozen_bytes':
        Path(native['independent_chat_support_srts'][0]['path']).write_text('tampered')
    elif damage == 'malformed':
        native['independent_chat_support_srts'] = None
    else:
        native['independent_chat_support_srts'] = []
        assert pipeline._source_stage_chat_support_srts({'native_checkpoint': native}, padded=padded) == []
        with pytest.raises(pipeline.CurrentTextContinuationError, match='CURRENT_SOURCE_STAGE_APPLIED_SUPPORT_DRIFT'):
            _consume(case, [])
        return
    with pytest.raises(pipeline.CurrentTextContinuationError):
        pipeline._source_stage_chat_support_srts({'native_checkpoint': native}, padded=padded)


def test_fresh_writer_does_not_promote_refined_or_language_witness(tmp_path):
    case = _case(tmp_path / 'original', 'superchat')
    padded, support, *_ = case
    padded.with_suffix('.asr_draft.srt').unlink()
    padded.with_suffix('.agy_refined.srt').write_text(support)
    _, native = _write(tmp_path / 'checkpoint', 'partial', case, padded)
    assert 'language_witness' in native
    assert native['independent_chat_support_srts'] == []
    assert pipeline._source_stage_chat_support_srts({'native_checkpoint': native}, padded=padded) == []


@pytest.mark.parametrize('lane', ['source', 'partial'])
def test_explicit_producer_support_preserves_its_exact_hash_bound_newlines(tmp_path, lane):
    case = _case(tmp_path / 'original', 'danmaku', support_newline='\r\n')
    padded, support, *_ = case
    _, native = _write(tmp_path / 'checkpoint', lane, case, padded,
        independent_chat_support_srts=[support])
    artifact = Path(native['independent_chat_support_srts'][0]['path'])
    assert artifact.read_bytes() == support.encode('utf-8')
    # Restore the previous reader's actual normalization mechanism: the same
    # stored producer receipt must reject the resulting changed input hash.
    normalized = artifact.read_text(encoding='utf-8')
    assert normalized != support
    with pytest.raises(pipeline.CurrentTextContinuationError, match='CURRENT_SOURCE_STAGE_APPLIED_SUPPORT_DRIFT'):
        _consume(case, [normalized])
    restored = pipeline._source_stage_chat_support_srts({'native_checkpoint': native}, padded=padded)
    assert restored == [support]
    _consume(case, restored)


@pytest.mark.parametrize('lane', ['source', 'partial'])
def test_normal_snapshot_reads_written_support_after_media_move(tmp_path, lane):
    fixture = _checkpoint(tmp_path)
    _make_source_stage_checkpoint(fixture)
    item, row = _ordinary_chat_read_fixture(fixture)
    root, cid, original = fixture['out_root'], fixture['cid'], fixture['padded']
    authority = json.loads(fixture['authority_path'].read_text())
    authority['applied'] = [row]
    fixture['authority_path'].write_text(json.dumps(authority))
    if lane == 'source':
        pointer = root / f'{cid}.source-text-checkpoint.json'
        checkpoint.write_source_text_checkpoint(pointer, candidate_id=cid,
            srt_path=root / 'padded.fresh.srt', authority_path=fixture['authority_path'],
            context_path=fixture['context_path'], padded=original)
    else:
        pointer = root / f'{cid}.partial-source-text-checkpoint.json'
        checkpoint.write_partial_source_text_checkpoint(pointer, candidate_id=cid,
            srt_text=fixture['srt'], authority=authority, final_review={'status': 'APPLIED'},
            context_path=fixture['context_path'], padded=original,
            source_language_witness_srt=fixture['srt'], protected_name_source_srt=fixture['srt'])
    moved = tmp_path / 'moved' / original.name
    moved.parent.mkdir()
    moved.write_bytes(original.read_bytes())
    provenance = json.loads(original.with_suffix('.provenance.json').read_text())
    provenance['output_path'] = str(moved.resolve())
    moved.with_suffix('.provenance.json').write_text(json.dumps(provenance))
    snapshot = pipeline._automatic_current_checkpoint_snapshot(spec=fixture['spec'],
        durations=[100], padded=moved, padded_dur=100, cid=cid, out_root=root)
    restored = pipeline._source_stage_chat_support_srts(snapshot, padded=moved)
    assert restored == [fixture['srt']]
    pipeline._validate_source_stage_chat_reads(snapshot['chat_authority']['applied'],
        srt_text=snapshot['srt_text'], clip_context=snapshot['clip_context'],
        authoritative_chat=[item], independent_support_srts=restored)
