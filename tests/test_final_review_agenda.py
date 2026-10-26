import copy
import hashlib

from src.autoslice.final_review_agenda import coalesce_exact_review_agenda
from src.autoslice.final_review_auditor import build_context_adjudication_request

SRT = '1\n00:00:00,000 --> 00:00:01,000\n前句\n\n2\n00:00:01,000 --> 00:00:02,000\n来吧来人放那\n\n3\n00:00:02,000 --> 00:00:03,000\n后句\n'


def nomination(**changes):
    return dict(cue_index=2, suspect='来吧来人放那', suggestion='',
                proposed_full_cue=None, repair_class='phonetic',
                base_text_sha256=hashlib.sha256('来吧来人放那'.encode()).hexdigest(),
                why='旧疑点' * 60, kind='context', evidence_cue_ids=[], **changes)


def test_collects_both_reasons_and_evidence_before_request_without_mutating_input():
    a = nomination()
    b = {**a, 'why': '新发现检查了词界', 'kind': 'nonword', 'evidence_cue_ids': [1, 3],
         'input_contract_normalizations': ['explicit_null_proposal_bound_to_current_whole_cue']}
    original = copy.deepcopy([a, b])
    agenda = coalesce_exact_review_agenda(SRT, [a, b])
    assert len(agenda) == 1
    assert [a, b] == original
    assert agenda[0]['evidence_cue_ids'] == [1, 3]
    # Use the real request builder's ordinary missing-proposal scaffold.
    from src.autoslice.final_review_agenda import reviewer_reason
    assert b['why'] in reviewer_reason(agenda[0], limit=120)
    assert a['why'] in reviewer_reason(agenda[0], limit=120)
    scaffold = {**agenda[0], 'proposed_full_cue': a['suspect'],
                'candidate_blind_observation_scaffolding': True,
                'suggestion': a['suspect'], 'span_start_codepoint': 0,
                'span_end_codepoint': len(a['suspect'])}
    request = build_context_adjudication_request(SRT, scaffold)
    assert b['why'] in request['reason'] and a['why'] in request['reason']
    assert request['evidence_cue_ids'] == [1, 3]


def test_different_proposal_source_audio_gate_or_invalid_binding_stays_separate():
    a = nomination()
    for change in [
        {'proposed_full_cue': '来吧来点放那', 'suggestion': '来吧来点放那'},
        {'candidate_provenance': {'source_media_sha256': 'new-source'}},
        {'base_text_sha256': 'wrong'},
        {'evidence_cue_ids': [999]},
        {'suspect': '放那'},
    ]:
        assert len(coalesce_exact_review_agenda(SRT, [a, {**a, **change}])) == 2


def test_normal_control_and_idempotent_agenda():
    a = nomination()
    assert coalesce_exact_review_agenda(SRT, [a]) == [a]
    b = {**a, 'why': '另一理由', 'evidence_cue_ids': [3]}
    merged = coalesce_exact_review_agenda(SRT, [a, b])
    assert coalesce_exact_review_agenda(SRT, merged) == merged


def test_pending_audio_obligation_survives_fresh_rediscovery():
    a = nomination()
    gate = {'status': 'UNCERTAIN', 'request': {'request_sha256': 'old-bound-request'},
            'witness_judge': {'needs_audio': True}}
    old = {**a, 'exact_release_adjudication': gate}
    for pair in ([old, a], [a, old]):
        merged = coalesce_exact_review_agenda(SRT, pair)
        assert len(merged) == 1
        assert merged[0]['exact_release_adjudication'] == gate
    other = {**a, 'exact_release_adjudication': {**gate, 'request': {'request_sha256': 'other'}}}
    assert len(coalesce_exact_review_agenda(SRT, [old, other])) == 2
