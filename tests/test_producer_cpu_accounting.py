"""Clock evidence survives the ordinary stage/exit writer and offline reader."""
import json
import math
import os
import threading

import pytest

from src.autoslice import producer_stage_log as stages, run_time_ledger as ledger
from tests.test_run_time_ledger import stamp


@pytest.mark.parametrize('failure', [False, True])
def test_live_exit_persists_cpu_without_changing_exception_or_stdout(tmp_path, capsys, failure):
    original = RuntimeError('original failure')
    try:
        with ledger.producer_run_ledger(out_root=tmp_path, cid='measured'):
            with stages.producer_stage('unit.cpu'):
                print('original stdout')
                if failure:
                    raise original
    except RuntimeError as error:
        assert failure and error is original
    result = json.loads((tmp_path / 'measured.time-ledger.json').read_text())
    assert all(math.isfinite(result['cpu'][key]) and result['cpu'][key] >= 0
               for key in ('process_s', 'reaped_children_user_s', 'reaped_children_system_s'))
    row, = result['spans']
    assert row['pid'] == os.getpid() and row['thread_id'] == threading.get_ident()
    assert row['process_cpu_s_start'] >= 0 and row['thread_cpu_s_start'] >= 0
    assert ('process_cpu_s' in row) is not failure
    assert ('thread_cpu_s' in row) is not failure
    assert capsys.readouterr().out == 'original stdout\n'


def test_interleaved_processes_and_nested_stages_keep_independent_clock_deltas(tmp_path):
    lines = [
        f'PRODUCER_STAGE {stamp(0)} START outer n=1 pid=10 thread_id=1 process_cpu_s=5 thread_cpu_s=2',
        f'PRODUCER_STAGE {stamp(1)} START inner n=1 pid=10 thread_id=1 process_cpu_s=6 thread_cpu_s=3',
        f'PRODUCER_STAGE {stamp(2)} START inner n=1 pid=20 thread_id=1 process_cpu_s=50 thread_cpu_s=20',
        f'PRODUCER_STAGE {stamp(3)} END inner n=1 pid=10 thread_id=1 process_cpu_s=8 thread_cpu_s=4 elapsed_s=2',
        f'PRODUCER_STAGE {stamp(4)} END inner n=1 pid=20 thread_id=1 process_cpu_s=53 thread_cpu_s=21 elapsed_s=2',
        f'PRODUCER_STAGE {stamp(5)} END outer n=1 pid=10 thread_id=1 process_cpu_s=9 thread_cpu_s=5 elapsed_s=5',
    ]
    sources = ledger.collect_sources(tmp_path, stage_lines=lines)
    result = ledger.build_ledger(sources, start=stamp(0), end=stamp(6))
    outer, inner, other = result['spans']
    assert [(r['process_cpu_s'], r['thread_cpu_s']) for r in (outer, inner, other)] == [(4, 3), (2, 1), (3, 1)]
    assert result['totals']['wall_s'] == 6
    assert result.get('cpu') is None  # Per-stage overlap cannot invent a run total.
    assert not result['coverage']['issues']


def test_legacy_or_invalid_cpu_clocks_remain_unknown(tmp_path):
    lines = [f'PRODUCER_STAGE {stamp(0)} START old n=1',
             f'PRODUCER_STAGE {stamp(2)} END old n=1 elapsed_s=2',
             f'PRODUCER_STAGE {stamp(3)} START bad n=1 process_cpu_s=nan thread_cpu_s=10',
             f'PRODUCER_STAGE {stamp(4)} END bad n=1 process_cpu_s=1 thread_cpu_s=9 elapsed_s=1']
    result = ledger.build_ledger(ledger.collect_sources(tmp_path, stage_lines=lines), start=stamp(0), end=stamp(5))
    assert all('process_cpu_s' not in row and 'thread_cpu_s' not in row for row in result['spans'])
    assert result.get('cpu') is None
