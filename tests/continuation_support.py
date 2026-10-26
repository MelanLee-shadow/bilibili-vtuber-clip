"""Synthetic subtitle/review chain for portable continuation contract tests."""
import hashlib

from src.autoslice.boundary_semantic_review import cue_grid_sha256
from src.autoslice.jingting_chunker import parse_srt_cues


def sha(text):
    return 'sha256:' + hashlib.sha256(text.encode()).hexdigest()


def srt(text='合成终稿句'):
    blocks = []
    for index in range(1, 20):
        start = (index - 1) * 2
        body = text if index == 7 else f'合成上下文第{index}句'
        blocks.append(f'{index}\n00:00:{start:02d},000 --> 00:00:{start + 1:02d},500\n{body}')
    return '\n\n'.join(blocks) + '\n'


def review():
    final = srt()
    grid = cue_grid_sha256(parse_srt_cues(final))
    passes = []
    for index, (before, after) in enumerate((('合成原句甲', '合成中间句'),
                                             ('合成中间句', '合成终稿句')), 1):
        repair = {
            'schema_version': 'exact-final-cpa-self-heal.v1', 'cue_index': 7,
            'before': before, 'after': after, 'before_sha256': sha(before), 'after_sha256': sha(after),
            'finding_sha256': 'sha256:' + 'd' * 64, 'request_sha256': 'sha256:' + 'e' * 64,
            'decision_authority': 'CPA_JUDGE', 'timing_immutable': True,
            'matched_start_ms': 12000, 'matched_end_ms': 13500,
            'mutation_authority': {'schema_version': 'subtitle-correction-mutation-authority.v1', 'status': 'PASS'},
            'judge': {'schema_version': 'subtitle-span-cpa-judge.v1', 'status': 'JUDGED',
                      'choice': 'PROPOSED', 'check_request_sha256': 'sha256:' + 'e' * 64},
            'acoustic_witness': {'schema_version': 'subtitle-span-acoustic-witness.v1',
                                 'status': 'OBSERVED', 'target_audible': True},
        }
        passes.append({'schema_version': 'exact-final-cpa-self-heal-pass.v1', 'pass_index': index,
                       'input_srt_sha256': sha(srt(before)), 'output_srt_sha256': sha(srt(after)),
                       'repairs': [repair]})
    endpoint = {'schema_version': 'talk-boundary-final-endpoint-binding.v1', 'status': 'PASS',
                'recommended_end_cue_index': 19, 'recommended_end_ms': 37500,
                'final_closure_cue_index': 19, 'final_snapped_end_ms': 37500,
                'final_start_ms': 0, 'final_end_ms': 37500,
                'semantic_cue_grid_sha256': grid, 'final_cue_grid_sha256': grid, 'reason_codes': []}
    return {
        'schema_version': 'final-review-audit.v2', 'status': 'CLEAN', 'release_gate': 'PASS',
        'reviewed_srt_sha256': sha(final), 'findings': [], 'validated_finding_count': 0,
        'resolved_findings': [], 'unresolved_findings_disclosed': [], 'reason_codes': [],
        'discovery': {'status': 'COMPLETE'}, 'correction_pass': {'applied_count': 0},
        'correction_mutation_authority': {'schema_version': 'subtitle-correction-mutation-audit.v1', 'status': 'PASS'},
        'exact_final_cpa_self_heal': {'schema_version': 'exact-final-cpa-self-heal-audit.v1',
                                    'status': 'PASS', 'final_srt_sha256': sha(final), 'passes': passes},
        'boundary_semantic_review': {
            'schema_version': 'talk-boundary-semantic-review.v1', 'status': 'PASS',
            'review_scope': 'final_delivery', 'candidate_id': 'relationship-candidate',
            'request_sha256': 'sha256:' + 'a' * 64, 'cue_grid_sha256': grid,
            'recommended_end_cue_index': 19, 'recommended_end_ms': 37500,
            'final_endpoint_binding': endpoint,
            'source_separation_witness': {'schema_version': 'talk-boundary-source-separation-witness.v1',
                'status': 'PASS', 'source_review_sha256': 'sha256:' + 'b' * 64,
                'source_request_sha256': 'sha256:' + 'c' * 64, 'source_cue_grid_sha256': grid,
                'source_final_start_ms': 0, 'source_final_end_ms': 37500, 'reason_codes': []},
        },
    }


def assert_text_recovery_keeps_release_blocked(tmp_path):
    """Exercise the normal gate with synthetic pending-boundary authority."""
    import pytest
    from src.autoslice import producer_package_finalization as finalization
    from src.autoslice.final_review_contract import FinalReviewContractError, validate_final_review_release
    from src.autoslice.final_review_meaning_policy import POLICY_VERSION

    subtitle = tmp_path / 'current.srt'
    subtitle.write_text('1\n00:00:00,000 --> 00:00:01,000\n保留正文\n')
    current_sha = 'sha256:' + hashlib.sha256(subtitle.read_bytes()).hexdigest()
    audit = {
        'schema_version': 'final-review-audit.v2', 'meaning_policy': POLICY_VERSION,
        'status': 'FLAGGED', 'release_gate': 'BLOCK',
        'reason_codes': ['FINAL_REVIEW_BOUNDARY_SEMANTIC_BLOCKED'],
        'reviewed_srt_sha256': current_sha, 'discovery': {'status': 'COMPLETE'},
        'correction_mutation_authority': {'schema_version': 'subtitle-correction-mutation-audit.v1', 'status': 'PASS'},
        'findings': [], 'validated_finding_count': 0,
        'boundary_semantic_review': {'status': 'BLOCK', 'reason_codes': ['CURRENT_TEXT_REQUIRES_SOURCE_REVIEW']},
    }
    kwargs = dict(cid='current', out_root=tmp_path, final_start=4000, final_end=5000,
        recut=finalization.CurrentTextReviewArtifacts(subtitle),
        chat_authority_audit={}, chat_authority_path=tmp_path / 'authority.json',
        run_exact_final_review=lambda *_: audit, text_recovery_only=True)
    result = finalization._run_exact_final_review_gate(**kwargs)
    assert result['pre_boundary_text_recovery']['status'] == 'TEXT_COMPLETE_BOUNDARY_PENDING'
    with pytest.raises(FinalReviewContractError, match='BOUNDARY_SEMANTIC_BLOCKED'):
        validate_final_review_release(result, expected_srt_sha256=current_sha)
    audit['reviewed_srt_sha256'] = 'sha256:' + '0' * 64
    with pytest.raises(SystemExit, match='SRT_BINDING_MISMATCH'):
        finalization._run_exact_final_review_gate(**kwargs)
