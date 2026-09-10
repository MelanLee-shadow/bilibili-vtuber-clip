"""ASR text must enter the actual CPA closed set without assuming AGY first."""
import json

from src.autoslice.acoustic_witness_adjudication import adjudicate_with_witness


def test_cpa_can_reject_native_transcript_and_choose_original_bcut():
    request = {
        'request_sha256': 'a' * 64,
        'current_cue': '初轮候选', 'proposed_cue': '誤った候補',
        'proposed_candidates': [{'text': '原始中文', 'source': {'kind': 'source_asr_draft'}}],
        'repair_class': 'spoken_unit',
        'whole_clip_current_srt': '1\n00:00:00,000 --> 00:00:02,000\n初轮候选\n',
    }
    witness = {
        'schema_version': 'subtitle-span-acoustic-witness.v1',
        'witness_protocol': 'candidate_blind_transcript', 'status': 'OBSERVED',
        'target_audible': True, 'candidate_exposure': 'none',
        'exact_transcript': '誤った候補', 'provider': 'mai',
    }
    prompts = []
    def cpa(prompt):
        prompts.append(prompt)
        assert '原始中文' in prompt and '誤った候補' in prompt and '初轮候选' in prompt
        assert '没有收到独立拼音证词' in prompt
        return json.dumps({'choice': 'PROPOSED', 'candidate_id': 'SOURCE_1',
                           'selected_candidate_text': '不允许自由编写的覆盖文字',
                           'reason': 'The original Chinese source fits the full context.'})
    applied, _, audit = adjudicate_with_witness(check_request=request, witness=witness, llm_call=cpa)
    assert applied
    assert audit['judge']['selected_candidate_text'] == '原始中文'
    assert audit['decision_authority'] == 'CPA_JUDGE'
    assert len(prompts) == 1


def test_manufactured_pinyin_on_asr_transcript_never_reaches_cpa():
    def forbidden(_):
        raise AssertionError('manufactured acoustic evidence reached CPA')
    applied, reason, _ = adjudicate_with_witness(
        check_request={'current_cue': '甲', 'proposed_cue': '乙'},
        witness={'schema_version': 'subtitle-span-acoustic-witness.v1',
                 'witness_protocol': 'candidate_blind_transcript', 'status': 'OBSERVED',
                 'target_audible': True, 'candidate_exposure': 'none',
                 'exact_transcript': '乙', 'heard_pinyin': 'yi', 'confidence': 1.0},
        llm_call=forbidden,
    )
    assert not applied
    assert reason == 'TRANSCRIPT_OBSERVATION_INVALID_KEEP_CURRENT'
