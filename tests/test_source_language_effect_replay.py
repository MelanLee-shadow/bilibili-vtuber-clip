"""Count changes may consume validated effects without weakening language checks."""
from copy import deepcopy
import hashlib

import pytest

from src.autoslice import producer_text_pipeline as pipeline


def _case():
    original = ('1\n00:00:01,000 --> 00:00:02,000\nTA喜欢这个\n\n'
                '2\n00:00:03,000 --> 00:00:04,000\n完全现场口令说\n\n'
                '3\n00:00:05,000 --> 00:00:06,500\n我可以唱这个吗\n\n'
                '4\n00:00:06,500 --> 00:00:07,000\n请\n')
    effects = [('applied', {'before': ['TA喜欢这个'], 'after': ['她喜欢这个'],
                           'matched_start_ms': 1000, 'matched_end_ms': 2000}),
               ('entity_repairs', {'before': ['完全现场口令说'], 'after': ['完全に無双状態'],
                                   'matched_start_ms': 3000, 'matched_end_ms': 4000})]
    modified = original.replace('TA喜欢这个', '她喜欢这个').replace('完全现场口令说', '完全に無双状態')
    authority = {'applied': [effects[0][1]], 'entity_repairs': [effects[1][1]]}
    current = pipeline._post_truth_release_hygiene(modified, authority)
    assert authority['release_grade_cue_merges'] == [
        {'block': 4, 'text': '请', 'action': 'MERGED_INTO_PREV'}]
    authority['final_source_language_preservation_audit'] = {
        'schema_version': 'source-language-preservation-audit.v1',
        'status': 'SKIPPED_CUE_COUNT_MISMATCH', 'draft_cue_count': 4,
        'final_cue_count': 3, 'reverted': [], 'reverted_count': 0,
        'unproven_foreign_introductions': [],
    }
    return original, current, authority, effects


def _replay(original, current, authority, effects):
    return pipeline._replay_typed_source_language_guard(
        authority, original_reference=original, srt_text=current,
        spec={}, durations=[8000], validated_effects=effects)


def test_validated_effects_recheck_original_grid_and_survive_cold_recovery():
    import json
    original, current, authority, effects = _case()
    saved = deepcopy(authority)
    assert _replay(original, current, authority, None) is None
    result = _replay(original, current, authority, effects)
    assert result['status'] == 'CLEAN'
    assert result['cue_binding'] == 'UNCHANGED_FOREIGN_CUE_INTERVALS'
    assert result['draft_cue_count'] == 4 and result['final_cue_count'] == 3
    assert result['typed_language_reference']['original_reference_sha256'] == hashlib.sha256(original.encode()).hexdigest()
    assert len(result['typed_language_reference']['effects']) == 2
    assert authority == saved
    authority['final_source_language_preservation_audit'] = json.loads(json.dumps(result))
    assert _replay(original, current.replace('我可以唱这个吗', '我能唱这个吗'), authority, effects) == result


@pytest.mark.parametrize('damage', ['missing_chat', 'missing_foreign', 'extra_foreign', 'count'])
def test_missing_effect_or_unowned_foreign_surface_cannot_pass(damage):
    original, current, authority, effects = _case()
    if damage == 'missing_chat':
        effects = effects[1:]
    elif damage == 'missing_foreign':
        effects = effects[:1]
    elif damage == 'extra_foreign':
        current = current.replace('我可以唱这个吗', 'new English speech')
    elif damage == 'count':
        authority['final_source_language_preservation_audit']['draft_cue_count'] += 1
    assert _replay(original, current, authority, effects) is None


@pytest.mark.parametrize('damage', ['word', 'time', 'deleted', 'duplicate_source', 'duplicate_effect', 'before'])
def test_changed_target_or_ambiguous_effect_is_rejected(damage):
    original, current, authority, effects = _case()
    if damage == 'word':
        current = current.replace('完全に無双状態', '完全に別の意味')
    elif damage == 'time':
        current = current.replace('00:00:03,000', '00:00:03,001')
    elif damage == 'deleted':
        current = current.replace('完全に無双状態', '字幕已删除')
    elif damage == 'duplicate_source':
        original += '\n5\n00:00:03,000 --> 00:00:04,000\n完全现场口令说\n'
    elif damage == 'duplicate_effect':
        effects.append(effects[-1])
    elif damage == 'before':
        effects[-1][1]['before'] = ['另一段原话']
    with pytest.raises(pipeline.CurrentTextContinuationError, match='TYPED_REFERENCE'):
        _replay(original, current, authority, effects)


def test_saved_reference_binding_is_recomputed():
    original, current, authority, effects = _case()
    result = _replay(original, current, authority, effects)
    result['typed_language_reference']['derived_reference_sha256'] = 'f' * 64
    authority['final_source_language_preservation_audit'] = result
    with pytest.raises(pipeline.CurrentTextContinuationError, match='BINDING_DRIFT'):
        _replay(original, current, authority, effects)
