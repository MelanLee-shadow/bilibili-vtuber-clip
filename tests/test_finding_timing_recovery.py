"""Durable repeated work is measured by attempts, not only its last span."""
from dataclasses import replace
import json

import pytest

from src.autoslice import finding_closure as core
from src.autoslice.finding_closure_shell import ClosureRunner, FileLedger
from src.autoslice.review_errors import ProviderFailure
from tests.test_finding_closure import Oracle, SRT
from tests.test_run_time_ledger import stamp


@pytest.mark.parametrize('durations', [(20, 30), (0, 30)])
def test_failed_work_attempts_survive_ordinary_resume_and_cold_summary(tmp_path, monkeypatch, durations):
    class WaitingOwner(Oracle):
        def first_scan(self, request):
            raise ProviderFailure('original owner pending', provider='recorded')

    initial = core.initial_state(source_sha256='a'*64, srt_text=SRT, scan_channels=('recorded',))
    root = tmp_path/'closure'
    ledger = FileLedger(root, initial)
    original = ledger.record_span
    attempts = []
    def clocked(span):
        if span.name == 'finding_closure.initial_scan':
            start = len(attempts) * 40
            duration = durations[len(attempts)]
            attempts.append(span.span_id)
        else:
            assert span.name == 'finding_closure.prefetch'
            start, duration = 0, 5
        original(replace(span, started_utc=stamp(start), ended_utc=stamp(start+duration)))
    monkeypatch.setattr(ledger, 'record_span', clocked)
    owner = WaitingOwner()
    for _ in durations:
        with pytest.raises(core.ClosureContractError):
            ClosureRunner(initial=initial, ledger=ledger, ports=owner).run()
    assert attempts[0] == attempts[1]
    before = {path: path.read_bytes() for path in (root/'events').glob('*.json')}
    FileLedger(root, initial, create=False).summarize(initial)
    result = json.loads((root/'timing-summary.json').read_text())
    assert result['workflow_critical_path_s'] == max(5, sum(durations))
    assert result['workflow_critical_path_basis']['complete'] is True
    assert result['workflow_critical_path_basis']['missing_effect_spans'] == 0
    assert before == {path:path.read_bytes() for path in before}
    assert owner.calls.count(('prefetch', None)) == 1


def test_committed_event_with_missing_clock_remains_partial(tmp_path):
    class WaitingOwner(Oracle):
        def first_scan(self, request):
            raise ProviderFailure('original owner pending', provider='recorded')
    initial = core.initial_state(source_sha256='a'*64, srt_text=SRT, scan_channels=('recorded',))
    root = tmp_path/'closure'; ledger = FileLedger(root, initial)
    with pytest.raises(core.ClosureContractError):
        ClosureRunner(initial=initial, ledger=ledger, ports=WaitingOwner()).run()
    spans = root/'time-spans.jsonl'
    spans.write_text(''.join(line+'\n' for line in spans.read_text().splitlines()
                            if json.loads(line)['name'] != 'finding_closure.initial_scan'))
    FileLedger(root, initial, create=False).summarize(initial)
    result = json.loads((root/'timing-summary.json').read_text())
    assert result['workflow_critical_path_basis']['missing_effect_spans'] == 1
    assert result['workflow_critical_path_basis']['complete'] is False
