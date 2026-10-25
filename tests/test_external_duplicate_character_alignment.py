"""ASR variation, monotone timing, and rejection controls for public clip comparison."""
import json
from pathlib import Path

import pytest

from src.autoslice.external_duplicate_check import Cue, compare
from tests.test_external_duplicate_check import OWN, PublicBilibili, ReplayClient, NOW, check, package


def fuzzy_track():
    # Same speech, a homophone and similar glyph; punctuation and cue breaks differ.
    return [Cue(1.5, 6.5, '今天来聊契约练爱！我觉得，这个故事很好看。'),
            Cue(7.5, 9.5, '最侯再补充一句话')]


def test_asr_substitutions_punctuation_and_segmentation_cover_every_sentence():
    result = compare(OWN, 9, fuzzy_track(), 10)
    assert result.decision == 'FULL_DUPLICATE'
    assert result.covered_sentences == result.total_sentences == 3
    assert result.coverage_ratio == 1
    assert .9 <= result.character_coverage_ratio < 1
    assert sum(r.substituted_characters for r in result.sentence_alignments) == 2
    assert all(r.covered for r in result.sentence_alignments)
    assert result.sentence_alignments[0].external_start_seconds == 1.5
    assert result.sentence_alignments[-1].external_end_seconds == 9.5


def test_fuzzy_prefix_only_reports_proven_extra_sentence_and_seconds():
    result = compare(OWN, 9, fuzzy_track()[:1], 7)
    assert result.decision == 'HAS_EXTRA'
    assert result.extra_sentences == result.unmatched_sentences == (OWN[-1],)
    assert result.extra_seconds == 3
    assert result.unmatched_seconds == 2


@pytest.mark.parametrize('order', [(2, 0, 1), (1, 0, 2), (2, 1, 0)])
def test_reordering_cannot_prove_complete_coverage(order):
    external = [Cue(i * 3 + 1.5, i * 3 + 3.5, OWN[n].text) for i, n in enumerate(order)]
    assert compare(OWN, 9, external, 12).decision != 'FULL_DUPLICATE'


def test_complementary_external_parts_are_never_combined():
    data = json.loads((Path(__file__).parent / 'fixtures/external_duplicate_check/protocol_cases.json').read_text())
    page = data['view']['data']['pages'][0]
    data['view']['data']['pages'] = [dict(page, cid=1), dict(page, cid=2)]
    api = PublicBilibili(ReplayClient(data), now=NOW)
    api.subtitles = lambda _bv, cid: [Cue(c.start + 1, c.end + 1, c.text) for c in (OWN[:2] if cid == 1 else OWN[1:])]
    result = check(package(), api)
    assert result['decision'] != 'FULL_DUPLICATE'
    assert result['block_upload'] is False


def test_fuzzy_repeated_occurrences_remain_ambiguous():
    track = fuzzy_track()
    track += [Cue(c.start + 12, c.end + 12, c.text) for c in track]
    assert compare(OWN, 9, track, 24).decision == 'UNKNOWN'


def test_internal_deletion_is_diagnostic_coverage_but_not_complete_media_proof():
    external = [Cue(c.start + 1, c.end + 1, c.text.replace('很好看', '好看')) for c in OWN]
    result = compare(OWN, 9, external, 10)
    assert result.decision == 'UNKNOWN'
    assert result.coverage_ratio == 1
    assert result.sentence_alignments[1].unmapped_characters == 1
    assert not result.extra_sentences and result.extra_seconds == 0


def test_internal_missing_sentence_is_not_extra_video_proof():
    result = compare(OWN, 9, [Cue(c.start + 1, c.end + 1, c.text) for c in (OWN[0], OWN[2])], 10)
    assert result.decision == 'UNKNOWN'
    assert result.unmatched_sentences == (OWN[1],)
    assert result.unmatched_seconds == 2
    assert not result.extra_sentences


@pytest.mark.parametrize('external', [
    [Cue(0, 1, '好的'), Cue(2, 3, '好得')],
    [Cue(1.5, 3.5, OWN[0].text)],
    [Cue(1.5, 9.5, '完全不相干的另外一个故事')],
])
def test_weak_evidence_stays_unknown(external):
    assert compare(OWN, 9, external, 10).decision == 'UNKNOWN'


def test_fuzzy_text_does_not_relax_exact_timing_requirement():
    track = fuzzy_track()
    track[-1] = Cue(track[-1].start, track[-1].end + .01, track[-1].text)
    assert compare(OWN, 9, track, 11).decision == 'UNKNOWN'


def test_high_overlap_but_different_speech_cannot_block():
    track = [Cue(c.start + 1, c.end + 1, c.text.replace('今天来聊', '今天不聊')) for c in OWN]
    result = compare(OWN, 9, track, 10)
    assert result.coverage_ratio == 1
    assert result.decision == 'UNKNOWN'


def test_similar_glyphs_are_covered_but_different_sounds_do_not_prove_full():
    own = (Cue(.5, 2.5, '今天已经准备开始讨论'), *OWN[1:])
    track = [Cue(c.start + 1, c.end + 1, c.text.replace('已经', '己经')) for c in own]
    result = compare(own, 9, track, 10)
    assert result.coverage_ratio == 1
    assert result.sentence_alignments[0].substituted_characters == 1
    assert result.decision == 'UNKNOWN'


def test_missing_phonetic_dependency_keeps_fuzzy_full_proof_unknown(monkeypatch):
    import src.autoslice.external_duplicate_check as module
    monkeypatch.setattr(module, 'text_pinyin_tokens', lambda _text: None)
    assert compare(OWN, 9, fuzzy_track(), 10).decision == 'UNKNOWN'
    assert compare(OWN, 9, [Cue(c.start + 1, c.end + 1, c.text) for c in OWN], 10).decision == 'FULL_DUPLICATE'


def test_unmatched_duration_counts_overlapping_cues_once():
    own = (*OWN, Cue(8, 9, '额外的内容'))
    result = compare(own, 9, fuzzy_track()[:1], 7)
    assert result.unmatched_seconds == 2.5
