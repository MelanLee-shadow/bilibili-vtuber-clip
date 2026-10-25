import copy
import hashlib

import pytest

from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.producer_text_pipeline import cue_grid_sha256
from test_final_source_language_replay import _produced


def _case(tmp_path, monkeypatch, *, inside):
    authority, source = _produced(tmp_path, monkeypatch)
    authority['post_transcript_entity_output_srt'] = source
    authority['post_transcript_entity_output_srt_sha256'] = hashlib.sha256(source.encode()).hexdigest()
    if inside:
        text = '1\n00:00:00,000 --> 00:00:01,660\n完全に無双状態\n'
        start, end = 3000, 4660
    else:
        text = '1\n00:00:00,000 --> 00:00:01,000\n先听这个\n'
        start, end = 0, 1000
    grid = cue_grid_sha256(parse_srt_cues(text))
    review = {'boundary_semantic_review': {
        'review_scope': 'final_delivery',
        'final_endpoint_binding': {'status': 'PASS', 'final_start_ms': 0,
            'final_end_ms': end - start, 'semantic_cue_grid_sha256': grid,
            'final_cue_grid_sha256': grid},
        'source_separation_witness': {'status': 'PASS', 'source_final_start_ms': start,
            'source_final_end_ms': end},
    }}
    return authority, text, review


@pytest.mark.parametrize('inside', [True, False])
def test_normal_saved_language_owner_replays_on_delivery_or_outside_story(tmp_path, monkeypatch, inside):
    authority, text, review = _case(tmp_path, monkeypatch, inside=inside)
    before = copy.deepcopy(authority)
    pipeline._continuation_validate_final_language_owner(
        authority['entity_repairs'][0], row_index=0, chat_authority=authority,
        srt_text=text, final_review=review,
    )
    assert authority == before
    assert 'judge' not in authority['entity_repairs'][0]


@pytest.mark.parametrize('inside', [True, False])
def test_normal_selection_outside_annotation_must_match_physical_story(tmp_path, monkeypatch, inside):
    authority, text, review = _case(tmp_path, monkeypatch, inside=inside)
    authority['entity_repairs'][0]['boundary_owner_rejection'] = 'OUTSIDE_IMMUTABLE_STORY_SCOPE'
    before = copy.deepcopy(authority)
    if inside:
        with pytest.raises(pipeline.CurrentTextContinuationError):
            pipeline._continuation_validate_final_language_owner(
                authority['entity_repairs'][0], row_index=0, chat_authority=authority,
                srt_text=text, final_review=review,
            )
    else:
        pipeline._continuation_validate_final_language_owner(
            authority['entity_repairs'][0], row_index=0, chat_authority=authority,
            srt_text=text, final_review=review,
        )
    assert authority == before


@pytest.mark.parametrize('drift', ['source_sha', 'source_text', 'receipt', 'registration',
                                   'delivery_text', 'clock', 'partial_cut'])
def test_language_owner_replay_rejects_changed_source_owner_or_delivery(tmp_path, monkeypatch, drift):
    authority, text, review = _case(tmp_path, monkeypatch, inside=True)
    if drift == 'source_sha':
        authority['post_transcript_entity_output_srt_sha256'] = '0' * 64
    elif drift == 'source_text':
        authority['post_transcript_entity_output_srt'] += '\n'
    elif drift == 'receipt':
        authority['entity_repairs'][0]['final_foreign_script_receipt_sha256'] = 'sha256:' + '1' * 64
    elif drift == 'registration':
        authority['final_foreign_script_cpa_surface_registrations'].clear()
    elif drift == 'delivery_text':
        text = text.replace('完全に無双状態', '不明的词')
        grid = cue_grid_sha256(parse_srt_cues(text))
        review['boundary_semantic_review']['final_endpoint_binding'].update(
            semantic_cue_grid_sha256=grid, final_cue_grid_sha256=grid)
    elif drift == 'clock':
        review['boundary_semantic_review']['source_separation_witness']['source_final_end_ms'] += 1
    else:
        review['boundary_semantic_review']['source_separation_witness'].update(
            source_final_start_ms=3500, source_final_end_ms=5160)
    before = copy.deepcopy(authority)
    with pytest.raises(pipeline.CurrentTextContinuationError):
        pipeline._continuation_validate_final_language_owner(
            authority['entity_repairs'][0], row_index=0, chat_authority=authority,
            srt_text=text, final_review=review,
        )
    assert authority == before
