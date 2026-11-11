"""A second cue enters through ordinary span and terminal producers."""
from copy import deepcopy
import socket

import pytest

from src.autoslice import span_finding_obligations as spans
from src.autoslice.finding_lifecycle import REVIEW_PENDING
from src.autoslice.review_evidence_store import load_review, persist_review
from src.autoslice.span_phoneme_resolution import digest, outside_question_packet, text_sha
from tests import test_span_obligation_closure as closure
from tests import test_span_phoneme_resolution as phoneme
from tests import test_terminal_rework2_obligations as terminal
from tests.test_terminal_contract_roundtrip import c_reply


def test_other_cue_ordinary_multiple_revisions_cold_roundtrip(tmp_path, monkeypatch):
    def forbid_network(*args, **kwargs):
        raise AssertionError('No network or real provider calls in offline test')
    monkeypatch.setattr(socket.socket, 'connect', forbid_network)
    monkeypatch.setattr(socket, 'create_connection', forbid_network)
    # An actual second cue with its own normal request and physical window.
    srt = ("1\n00:00:00,000 --> 00:00:01,000\n开头\n\n"
           + closure._SRT.replace("1\n00:00:01", "2\n00:00:01", 1)
           .replace("2\n00:00:04", "3\n00:00:04", 1)
           .replace("3\n00:00:05", "4\n00:00:05", 1)
           .replace("4\n00:00:07", "5\n00:00:07", 1))
    monkeypatch.setattr(phoneme, 'srt', lambda text: srt.replace('你想，甲0呀', text))
    _, adjudication, _, _, _ = phoneme.run_case(
        model=phoneme.model_for(question_span=[0, 2]), finding_extra={'cue_index': 2})
    packet = outside_question_packet(adjudication['request'], adjudication['verdict'],
                                     adjudication['witness_judge']['judge'])
    assert packet is not None
    authority = {'schema_version': 'terminal-rework2-test-authority.v1',
                 'span_unapplied_questions': [{'after_repair_count': 0,
                   'repair_prefix_sha256': digest([]), 'packet': packet}]}
    monkeypatch.setattr(terminal, '_SRT', srt)
    monkeypatch.setattr(terminal, '_FINDING', {**terminal._FINDING, 'cue_index': 2})
    plan = terminal._assisted_plan()
    plan.update(first_cue=2, last_cue=3, context_refs=['cue:4'])
    monkeypatch.setattr(terminal, '_assisted_plan', lambda: plan)
    monkeypatch.setattr(closure, '_SRT', srt)

    pending = spans.pending_findings(srt, authority)
    assert len(pending) == 1 and pending[0]['cue_index'] == 2
    adj, _ = terminal._terminal_audit(tmp_path / 'first', monkeypatch, c_reply)
    row = {**pending[0], **adj['rebuilt_finding'],
           'origin_span_obligation': pending[0]['origin_span_obligation'],
           'exact_release_adjudication': adj}
    row.pop('lifecycle', None)
    first = spans.enforce_review(srt, authority, closure.review(row))
    path = tmp_path / 'first.json'
    persist_review(path, first)
    cold = load_review(path)
    spans.validate_release_obligations(cold)
    history = deepcopy(cold['span_obligation_review']['closures'])
    authority.update(span_obligation_closures=history, final_review_audit=cold)
    assert not spans.pending_findings(srt, authority)

    changed = srt.replace('中间', '中间文字修订')
    pending = spans.pending_findings(changed, authority)
    assert pending and pending[0]['cue_index'] == 2
    empty = closure.review(row)
    empty.update(reviewed_srt_sha256='sha256:' + text_sha(changed),
                 unresolved_findings_disclosed=[], findings=[])
    waiting = spans.enforce_review(changed, authority, empty)
    assert waiting['release_gate'] == 'BLOCK'
    assert waiting['span_obligation_review']['closures'] == history
    path = tmp_path / 'waiting.json'
    persist_review(path, waiting)
    with pytest.raises(ValueError, match=REVIEW_PENDING):
        spans.validate_release_obligations(load_review(path))

    monkeypatch.setattr(terminal, '_SRT', changed)
    adj, _ = terminal._terminal_audit(tmp_path / 'second', monkeypatch, c_reply)
    row = {**pending[0], **adj['rebuilt_finding'],
           'origin_span_obligation': pending[0]['origin_span_obligation'],
           'exact_release_adjudication': adj}
    row.pop('lifecycle', None)
    audit = closure.review(row)
    audit['reviewed_srt_sha256'] = 'sha256:' + text_sha(changed)
    closed = spans.enforce_review(changed, authority, audit)
    assert closed['span_obligation_review']['closures'][:len(history)] == history
    path = tmp_path / 'closed.json'
    persist_review(path, closed)
    cold = load_review(path)
    spans.validate_release_obligations(cold)
    authority.update(span_obligation_closures=cold['span_obligation_review']['closures'],
                     final_review_audit=cold)
    assert spans.pending_findings(changed.replace('开头', '开头又改了'), authority)
