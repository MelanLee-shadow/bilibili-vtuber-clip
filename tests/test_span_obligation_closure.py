"""Ordinary span birth -> terminal B/C -> persistence -> release consumer."""
from copy import deepcopy
import json
import socket

import pytest

from src.autoslice import span_finding_obligations as spans
from src.autoslice.final_review_contract import is_release_resolved_keep
from src.autoslice.finding_lifecycle import classify, release_reason_codes, REVIEW_PENDING
from src.autoslice.review_evidence_store import persist_review, load_review
from src.autoslice.span_phoneme_resolution import digest, text_sha
from tests.test_terminal_rework2_obligations import (
    _terminal_audit, _real_unapplied_authority, _supported, _SRT,
)
from tests.test_terminal_contract_roundtrip import c_reply


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('No real provider or network calls in closure tests')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)


def case(tmp_path, monkeypatch, reply=c_reply, *, question_index=0):
    adj, _ = _terminal_audit(tmp_path, monkeypatch, reply)
    authority = _real_unapplied_authority()
    authority['span_unapplied_questions'] = [authority['span_unapplied_questions'][question_index]]
    pending = spans.pending_findings(_SRT, authority)[0]
    row = {**pending, **adj['rebuilt_finding'],
           'origin_span_obligation': pending['origin_span_obligation'],
           'exact_release_adjudication': adj}
    row.pop('lifecycle', None)
    return authority, row


def review(row):
    return {'schema_version': 'final-review-audit.v2',
            'meaning_policy': 'contextual-meaning-equivalence.v1',
            'reviewed_srt_sha256': 'sha256:' + text_sha(_SRT),
            'status': 'CLEAN', 'release_gate': 'PASS', 'reason_codes': [],
            'findings': [], 'resolved_findings': [], 'unresolved_findings_disclosed': [row]}


@pytest.mark.parametrize('reply', [c_reply, _supported])
@pytest.mark.parametrize('question_index', [0, 1])
def test_shared_keep_closes_birth_and_survives_cold_release(reply, question_index, tmp_path, monkeypatch):
    authority, row = case(tmp_path, monkeypatch, reply, question_index=question_index)
    assert is_release_resolved_keep(row)
    before = deepcopy(row)
    enforced = spans.enforce_review(_SRT, authority, review(row))
    assert not enforced['findings']
    assert all(r['status'] == 'RESOLVED' for r in enforced['span_obligation_review']['states'])
    path = tmp_path / 'review.json'
    persist_review(path, enforced)
    cold = load_review(path)
    spans.validate_release_obligations(cold)
    assert not release_reason_codes(cold['findings'])
    assert classify(cold['unresolved_findings_disclosed'][0])['state'] == 'KEEP'
    assert row == before
    # The same current row is not a new review obligation on a second pass.
    again = spans.enforce_review(_SRT, authority, deepcopy(cold))
    assert not again['findings']


@pytest.mark.parametrize('damage', [
    'meaning_reversal', 'critical', 'no_exhaustion', 'no_proof', 'other_cue',
    'other_srt', 'missing_risk', 'anchor_hash',
])
def test_sub18_rejections_retain_real_answer_and_recovery(damage, tmp_path, monkeypatch):
    authority, row = case(tmp_path, monkeypatch)
    adj = row['exact_release_adjudication']
    receipt = adj['sub18_context_meaning_equivalent_keep']
    proof = receipt['no_gain_proof']
    if damage in {'meaning_reversal', 'critical'}:
        row['content_risk'] = 'meaning_reversal' if damage == 'meaning_reversal' else 'factual_error'
    elif damage == 'no_exhaustion':
        proof.pop('material_exhaustion')
    elif damage == 'no_proof':
        receipt.pop('no_gain_proof')
    elif damage == 'other_cue':
        proof['anchor']['current_cue_sha256'] = text_sha('另一句')
    elif damage == 'other_srt':
        row['current_content_risk']['reviewed_srt_sha256'] = 'sha256:' + '0' * 64
    elif damage == 'missing_risk':
        row.pop('current_content_risk')
    else:
        adj['request']['base_text_sha256'] = '0' * 64
    if damage in {'no_exhaustion', 'other_cue'}:
        proof['proof_sha256'] = digest({k: v for k, v in proof.items() if k != 'proof_sha256'})
        receipt['no_gain_proof_sha256'] = proof['proof_sha256']
        receipt['material_exhaustion'] = proof.get('material_exhaustion')
    if damage != 'anchor_hash':
        assert not is_release_resolved_keep(row)
    # The outer request anchor is the obligation consumer's binding guard;
    # the shared keep predicate reopens the terminal request's own proof.
    enforced = spans.enforce_review(_SRT, authority, review(row))
    assert enforced['findings'] and enforced['release_gate'] == 'BLOCK'
    pending = enforced['findings'][0]
    history = pending['span_obligation_recovery']
    assert history['latest_adjudication_sha256'] == digest(adj)
    assert history['latest_adjudication'] == adj
    assert history['reason_code'] and history['owner']
    assert pending['lifecycle']['state'] != 'KEEP'
    assert pending['lifecycle']['detail']
    assert pending['lifecycle']['new_model_call_authorized'] is False
    assert release_reason_codes([pending])
    with pytest.raises(ValueError, match=REVIEW_PENDING):
        spans.validate_release_obligations(enforced)
    path = tmp_path / 'review.json'
    persist_review(path, enforced)
    cold = load_review(path)
    regenerated = spans.pending_findings(_SRT, {'final_review_audit': cold,
        'span_unapplied_questions': authority['span_unapplied_questions']})[0]
    assert regenerated['span_obligation_recovery']['latest_adjudication_sha256'] == digest(adj)
    assert regenerated['lifecycle']['state'] != 'KEEP'


def test_current_pending_row_and_latest_answer_share_history(tmp_path, monkeypatch):
    authority, row = case(tmp_path, monkeypatch)
    row['content_risk'] = 'meaning_reversal'
    stale = deepcopy(row)
    stale['exact_release_adjudication']['reason_code'] = 'OLD_ANSWER'
    authority['final_review_audit'] = review(stale)
    audit = review(row)
    audit['findings'] = spans.pending_findings(_SRT, authority)
    enforced = spans.enforce_review(_SRT, authority, audit)
    history = enforced['findings'][0]['span_obligation_recovery']
    assert history['latest_adjudication_sha256'] == digest(row['exact_release_adjudication'])


def test_shared_lifecycle_does_not_reclassify_existing_routes(tmp_path, monkeypatch):
    from src.autoslice.finding_lifecycle import annotate
    authority, row = case(tmp_path, monkeypatch)
    assert classify(row)['state'] == 'KEEP'
    edit = deepcopy(row)
    adj = edit['exact_release_adjudication']
    adj.update(repaired=True, mutation_authority={'status': 'PASS'})
    assert classify(edit)['state'] == 'EDIT_REVIEW'
    raw = spans.pending_findings(_SRT, authority)[0]
    assert annotate(raw)['lifecycle']['state'] == 'AWAITING_EVIDENCE'


def test_keep_cannot_be_carried_to_another_whole_srt(tmp_path, monkeypatch):
    authority, row = case(tmp_path, monkeypatch)
    changed_srt = _SRT.replace('中间', '另一句')
    audit = review(row)
    audit['reviewed_srt_sha256'] = 'sha256:' + text_sha(changed_srt)
    enforced = spans.enforce_review(changed_srt, authority, audit)
    assert enforced['findings']
    good = spans.enforce_review(_SRT, authority, review(row))
    good['reviewed_srt_sha256'] = 'sha256:' + text_sha(changed_srt)
    proof = good['span_obligation_review']
    proof['srt_sha256'] = text_sha(changed_srt)
    proof['receipt_sha256'] = digest({k: v for k, v in proof.items() if k != 'receipt_sha256'})
    with pytest.raises(ValueError, match='SPAN_KEEP_CLOSURE_CURRENT_SRT_DRIFT'):
        spans.validate_release_obligations(good)


@pytest.mark.parametrize('question_index,neighbor', [(0, '别处第一轮修改'), (1, '未见的邻句修改')])
def test_historical_sub18_reopens_then_new_current_review_closes(
    question_index, neighbor, tmp_path, monkeypatch
):
    import tests.test_terminal_rework2_obligations as terminal_fixture

    authority, old_row = case(tmp_path / 'old', monkeypatch, question_index=question_index)
    old = spans.enforce_review(_SRT, authority, review(old_row))
    old_path = tmp_path / 'old.json'
    persist_review(old_path, old)
    cold_old = load_review(old_path)
    spans.validate_release_obligations(cold_old)
    old_closures = deepcopy(cold_old['span_obligation_review']['closures'])
    authority['span_obligation_closures'] = old_closures
    authority['final_review_audit'] = cold_old

    # The ordinary continuation on unchanged text remains closed.
    assert not spans.pending_findings(_SRT, authority)
    changed = _SRT.replace('中间', neighbor)
    pending = spans.pending_findings(changed, authority)
    assert pending and pending[0]['span_obligation_recovery']['reason_code'] == 'SPAN_OBLIGATION_CURRENT_SRT_DRIFT'
    next_audit = review(old_row)
    next_audit.update(reviewed_srt_sha256='sha256:' + text_sha(changed),
                      unresolved_findings_disclosed=[], findings=[])
    waiting = spans.enforce_review(changed, authority, next_audit)
    assert waiting['span_obligation_review']['closures'] == old_closures
    assert waiting['span_obligation_review']['states'][0]['status'] == 'UNRESOLVED'
    assert waiting['release_gate'] == 'BLOCK' and waiting['findings']
    old_again = review(old_row)
    old_again['reviewed_srt_sha256'] = 'sha256:' + text_sha(changed)
    rejected = spans.enforce_review(changed, authority, old_again)
    assert rejected['span_obligation_review']['closures'] == old_closures
    assert rejected['release_gate'] == 'BLOCK' and rejected['findings']
    path = tmp_path / 'waiting.json'
    persist_review(path, waiting)
    cold = load_review(path)
    with pytest.raises(ValueError, match=REVIEW_PENDING):
        spans.validate_release_obligations(cold)
    assert cold['span_obligation_review']['closures'] == old_closures

    # A new ordinary terminal decision is built from the new whole SRT.
    monkeypatch.setattr(terminal_fixture, '_SRT', changed)
    new_adj, _ = _terminal_audit(tmp_path / 'new', monkeypatch, c_reply)
    new_row = {**pending[0], **new_adj['rebuilt_finding'],
               'origin_span_obligation': pending[0]['origin_span_obligation'],
               'exact_release_adjudication': new_adj}
    new_row.pop('lifecycle', None)
    new_audit = review(new_row)
    new_audit['reviewed_srt_sha256'] = 'sha256:' + text_sha(changed)
    closed = spans.enforce_review(changed, authority, new_audit)
    assert closed['span_obligation_review']['closures'][:len(old_closures)] == old_closures
    assert len(closed['span_obligation_review']['closures']) == len(old_closures) + 1
    assert not closed['findings']
    path = tmp_path / 'closed.json'
    persist_review(path, closed)
    spans.validate_release_obligations(load_review(path))

    # A second unrelated revision invalidates the newer closure too.
    authority['span_obligation_closures'] = closed['span_obligation_review']['closures']
    authority['final_review_audit'] = closed
    later = changed.replace('刚才只是在哼唱', '后来又在哼唱')
    assert spans.pending_findings(later, authority)
    assert len(authority['span_obligation_closures']) == len(old_closures) + 1


@pytest.mark.parametrize('damage', ['wrong_clock', 'missing_no_gain', 'meaning_reversal'])
def test_historical_closure_is_not_an_exemption_from_integrity(damage, tmp_path, monkeypatch):
    authority, row = case(tmp_path, monkeypatch)
    old = spans.enforce_review(_SRT, authority, review(row))
    closures = deepcopy(old['span_obligation_review']['closures'])
    closure = closures[0]
    if damage == 'wrong_clock':
        closure['adjudication']['request']['source_media_timeline_offset_ms'] = 1
    elif damage == 'missing_no_gain':
        closure['adjudication']['sub18_context_meaning_equivalent_keep'].pop('no_gain_proof')
    else:
        closure['finding']['content_risk'] = 'meaning_reversal'
    authority['span_obligation_closures'] = closures
    with pytest.raises(ValueError, match='SPAN_KEEP_CLOSURE_INVALID'):
        spans.pending_findings(_SRT.replace('中间', '已修改'), authority)


def test_validated_recovery_leaves_old_pending_state(tmp_path, monkeypatch):
    authority, row = case(tmp_path, monkeypatch)
    broken = deepcopy(row)
    broken['content_risk'] = 'meaning_reversal'
    failed = spans.enforce_review(_SRT, authority, review(broken))
    pending = failed['findings'][0]
    assert pending['lifecycle']['state'] != 'KEEP'
    recovered = {**pending, **row}
    recovered['span_obligation_recovery'] = pending['span_obligation_recovery']
    fixed = spans.enforce_review(_SRT, authority, review(recovered))
    assert not fixed['findings']
    spans.validate_release_obligations(fixed)
    kept = fixed['unresolved_findings_disclosed'][0]
    assert classify(kept)['state'] == 'KEEP'


def test_full_audio_closure_keeps_its_existing_scope_when_a_neighbor_changes(tmp_path, monkeypatch):
    from src.autoslice.final_review_meaning_policy import bind_current_risk
    authority, row = case(tmp_path, monkeypatch, _supported)
    row.update(content_risk='noncritical_uncertainty', risk_basis='当前整句已有完整听音支持。')
    row['current_content_risk'] = bind_current_risk(row, srt_text=_SRT, cue_count=4,
        prompt='synthetic whole-current review', meaning_equivalence={
            'status': 'EQUIVALENT', 'current_meaning': '当前语句',
            'alternative_meaning': '当前语句', 'shared_meaning': '当前语句',
            'uncertain_detail': '字面呈现', 'context_basis': '当前语句已有完整听音支持。',
            'evidence_cue_ids': [1, 2]})
    assert is_release_resolved_keep(row)
    changed_srt = _SRT.replace('中间', '另一句')
    audit = review(row)
    audit['reviewed_srt_sha256'] = 'sha256:' + text_sha(changed_srt)
    enforced = spans.enforce_review(changed_srt, authority, audit)
    assert not enforced['findings']
    spans.validate_release_obligations(enforced)
    # This only preserves the independent audio obligation's old scope.
    # G still checks the displayed row's risk against the current whole SRT.


def test_saved_generated_pending_is_retracted_by_ordinary_current_keep(tmp_path, monkeypatch):
    from src.autoslice.finding_lifecycle import REVIEW_PENDING
    authority, row = case(tmp_path, monkeypatch)
    audit = review(row)
    audit.update(findings=spans.pending_findings(_SRT, authority), status='FLAGGED',
                 release_gate='BLOCK', reason_codes=[REVIEW_PENDING])
    frozen = deepcopy(audit)
    assert len(audit['findings']) == 1 and not audit['findings'][0].get('exact_release_adjudication')
    enforced = spans.enforce_review(_SRT, authority, audit)
    assert not enforced['findings']
    assert enforced['status'] == 'CLEAN' and enforced['release_gate'] == 'PASS'
    assert REVIEW_PENDING not in enforced['reason_codes']
    spans.validate_release_obligations(enforced)
    assert len(frozen['findings']) == 1


@pytest.mark.parametrize('protect', ['risk', 'real_answer', 'stale_binding',
                                    'new_proposal', 'wrong_offset', 'different_span'])
def test_retraction_preserves_real_or_unbound_findings(protect, tmp_path, monkeypatch):
    authority, row = case(tmp_path, monkeypatch)
    audit = review(row)
    pending = spans.pending_findings(_SRT, authority)[0]
    if protect == 'risk':
        pending['content_risk'] = 'meaning_reversal'
    elif protect == 'real_answer':
        pending['exact_release_adjudication'] = {'status': 'UNRESOLVED', 'repaired': False}
    elif protect == 'stale_binding':
        pending['base_text_sha256'] = '0' * 64
    elif protect == 'new_proposal':
        pending['proposed_full_cue'] = '这是后来新增的候选'
    elif protect == 'wrong_offset':
        pending['origin_span_obligation']['source_offset_ms'] = 1
    else:
        pending['span_end_codepoint'] += 1
    audit['findings'] = [pending]
    enforced = spans.enforce_review(_SRT, authority, audit)
    assert enforced['findings']


def test_new_real_answer_can_inherit_history_without_becoming_an_old_view(tmp_path, monkeypatch):
    authority, row = case(tmp_path / 'old', monkeypatch)
    row['content_risk'] = 'meaning_reversal'
    authority['final_review_audit'] = review(row)
    pending = spans.pending_findings(_SRT, authority)[0]
    def incomplete_reply(data, refs):
        return {**c_reply(data, refs), 'meaning_equivalence': None}
    new, _ = _terminal_audit(tmp_path / 'new', monkeypatch, incomplete_reply)
    assert not is_release_resolved_keep({**row, 'exact_release_adjudication': new})
    current = {**pending, 'exact_release_adjudication': new}
    audit = review(row)
    audit['findings'] = [current]
    enforced = spans.enforce_review(_SRT, authority, audit)
    retained = enforced['findings'][0]['span_obligation_recovery']
    assert retained['latest_adjudication_sha256'] == digest(new)


def test_reanchored_history_survives_publication_and_a_fresh_empty_agenda(tmp_path, monkeypatch):
    authority, row = case(tmp_path, monkeypatch)
    authority['final_review_audit'] = review(row)
    blocks = []
    for number, block in enumerate(_SRT.strip().split('\n\n'), 2):
        lines = block.splitlines()
        lines[0] = str(number)
        blocks.append('\n'.join(lines))
    shifted = '1\n00:00:00,000 --> 00:00:01,000\n新前句\n\n' + '\n\n'.join(blocks) + '\n'
    merged = spans.merge_pending(shifted, authority, [deepcopy(row)])
    assert merged[0]['cue_index'] == 2 and not merged[0].get('exact_release_adjudication')
    source_hash = merged[0]['span_obligation_recovery']['latest_adjudication_sha256']
    path = tmp_path / 'reanchored.json'
    persist_review(path, {**authority, 'final_review_audit': {'findings': merged}})
    cold = load_review(path)
    next_row = spans.pending_findings(shifted, cold)[0]
    assert next_row['span_obligation_recovery']['latest_adjudication_sha256'] == source_hash
    assert next_row['lifecycle']['state'] == 'AWAITING_EVIDENCE'
    assert next_row['lifecycle']['new_model_call_authorized'] is False
