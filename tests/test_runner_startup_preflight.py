"""Caller contract tests: run after the A serial startup-callers patch is applied."""
from __future__ import annotations

from copy import deepcopy
from contextlib import nullcontext
import sys

import pytest

from scripts import session_autoslice as runner
from src.autoslice import producer_request as preflight


def _health_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'BASE', tmp_path)
    monkeypatch.setattr(runner, 'REPO_ROOT', tmp_path)
    profile = tmp_path / 'voiceprint_profile.v1.json'
    profile.write_text('{}')
    monkeypatch.setattr(runner, 'HOST_VOCAL_PROFILE', profile)
    monkeypatch.setattr(runner, 'profile_asset_file', lambda key: profile)
    monkeypatch.setattr(runner, 'load_selected_selection_calibration_policy', lambda: None)
    for name in ['scripts/produce_slice_package.py', 'src/autoslice/source_context_executor.py']:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# fixture')
    monkeypatch.setattr(runner, 'SPEAKER_MODE', 'uniform_host')
    monkeypatch.setattr(preflight.shutil, 'which', lambda command: sys.executable)


def _date_fixture(tmp_path, monkeypatch, state):
    _health_fixture(tmp_path, monkeypatch)
    events = []
    monkeypatch.setattr(runner, 'resume_prepared_batch_if_present', lambda **kw: events.append('resume') or {'saved': True})
    monkeypatch.setattr(runner, 'initialize_date_state', lambda *a: state)
    monkeypatch.setattr(runner, 'pre_dispatch_block_message', lambda *a, **k: None)
    monkeypatch.setattr(runner.historical_failed_talk_scope, 'freeze', lambda *a, **k: None)
    monkeypatch.setattr(runner, 'write_state', lambda *a: events.append('persist'))
    monkeypatch.setattr(runner, 'write_alert', lambda *a: None)
    monkeypatch.setattr(runner, 'log', lambda *a: None)
    return events


def test_date_pause_keeps_retry_and_resume_precedes_new_health_then_next_tick_advances(tmp_path, monkeypatch):
    state = {'status': 'queued', 'pending_talk': [{'cid': 'preflight', 'talk_transient_retry_count': 3, 'talk_repair_retry_count': 2}]}
    events = _date_fixture(tmp_path, monkeypatch, state)
    monkeypatch.setattr(runner, 'SPEAKER_MODE', 'required')
    selected = tmp_path / 'selected-python'
    monkeypatch.setenv('AUTOSLICE_SPEAKER_PYTHON', str(selected))
    monkeypatch.setattr(preflight.shutil, 'which', lambda command: None if str(command) == str(selected) else sys.executable)
    original = deepcopy(state['pending_talk'])
    def after_health(*args, **kwargs):
        events.append('after-health')
        raise RuntimeError('stop before any source/provider work')
    monkeypatch.setattr(runner, 'recover_finalized_legacy_hls', after_health)
    runner.process_date('2026-10-13')
    assert state['status'] == 'paused_runtime_invalid'
    assert 'speaker_python' in state['runtime_error']
    assert state['pending_talk'] == original
    assert events[0] == 'resume' and 'after-health' not in events
    monkeypatch.setattr(preflight.shutil, 'which', lambda command: sys.executable)
    with pytest.raises(RuntimeError, match='stop before'):
        runner.process_date('2026-10-13')
    assert state['pending_talk'] == original and 'runtime_error' not in state
    assert events[-1] == 'after-health'


def test_song_only_prerequisite_retains_queue_and_advances_after_repair(tmp_path, monkeypatch):
    state = {'status': 'queued', 'pending_talk': [], 'pending_song': [{'cid': 'song', 'transient_retry_count': 2}], 'picks': [], 'songs': []}
    _date_fixture(tmp_path, monkeypatch, state)
    selected = tmp_path / 'vocal-python'
    monkeypatch.setattr(runner, 'HOST_VOCAL_PYTHON', selected)
    monkeypatch.setattr(preflight.shutil, 'which', lambda command: None if str(command) == str(selected) else sys.executable)
    monkeypatch.setattr(runner, 'recover_finalized_legacy_hls', lambda *a, **k: [])
    monkeypatch.setattr(runner, 'audit_finalized_recording_inventory', lambda *a, **k: {'can_select': True})
    monkeypatch.setattr(runner.historical_failed_talk_scope, 'annotate_sessions', lambda *a, **k: 0)
    monkeypatch.setattr(runner, 'maintain_selected_source_fact_recovery', lambda **k: (0, 0, 0, 0, False))
    monkeypatch.setattr(runner.historical_failed_talk_scope, 'work_flags', lambda *a, **k: (False, True, False))
    monkeypatch.setattr(runner, 'cpa_healthy', lambda: True)
    monkeypatch.setattr(runner.historical_failed_talk_scope, 'discover', lambda *a, **k: None)
    monkeypatch.setattr(runner, 'suppress_exact_talk_recovery_song_work', lambda *a, **k: None)
    monkeypatch.setattr(runner, 'session_sealed', lambda *a: True)
    monkeypatch.setattr(runner.historical_failed_talk_scope, 'refresh_scorecards', lambda *a: 0)
    monkeypatch.setattr(runner.historical_failed_talk_scope, 'prioritize_and_capture', lambda *a: [])
    monkeypatch.setattr(runner, '_exact_talk_contract_ids', lambda *a: [])
    monkeypatch.setattr(runner.historical_failed_talk_scope, 'prepare_production_context', lambda *a: (True, None))
    monkeypatch.setattr(runner, 'producer_stop_requested', lambda: False)
    monkeypatch.setattr(runner, 'exclusive_runner_commit', lambda *a: nullcontext())
    monkeypatch.setattr(runner.runner_state_writeback, 'read_exact_state_preimage', lambda *a, **k: b'{}')
    calls = []
    def dispatch(**kwargs):
        calls.append(kwargs['lane'])
        raise RuntimeError('stop before producer')
    monkeypatch.setattr(runner, 'dispatch_prepared_lane', dispatch)
    original = deepcopy(state['pending_song'])
    runner.process_date('2026-10-13')
    assert not calls and state['pending_song'] == original
    assert state['status'] == 'paused_runtime_invalid'
    assert 'host_vocal_python' in state['runtime_error']
    monkeypatch.setattr(preflight.shutil, 'which', lambda command: sys.executable)
    runner.process_date('2026-10-13')
    assert calls == ['song'] and state['pending_song'] == original


@pytest.mark.parametrize('diagnostic', ['RUNTIME_PREREQUISITE: speaker_python: launch failed', "FileNotFoundError: [Errno 2] No such file or directory: 'ffmpeg'"])
def test_typed_or_media_launch_race_is_infrastructure_not_content_retry(diagnostic):
    classified = runner.classify_talk_failure(diagnostic)
    assert classified['failure_kind'] == 'runtime_prerequisite'
    assert classified['failure_stage'] == 'producer_preflight'
    assert classified['failure_recoverable'] is True
    classified = runner.classify_talk_failure("FileNotFoundError: [Errno 2] No such file or directory: '/recordings/missing.mp4'")
    assert classified['failure_kind'] == 'producer_error'


def test_child_export_overrides_stale_relative_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'BASE', tmp_path)
    monkeypatch.setenv('AUTOSLICE_BASE', 'stale-relative')
    monkeypatch.setattr(runner, 'load_env_file', lambda *a, **k: {})
    monkeypatch.setattr(runner, 'load_gemini_credentials', lambda: {})
    assert runner.child_env()['AUTOSLICE_BASE'] == str(tmp_path)


def test_absolute_base_pins_parent_child_and_relative_input_is_rejected(tmp_path, monkeypatch):
    assert preflight.resolve_runtime_base(tmp_path) == tmp_path
    monkeypatch.chdir(tmp_path)
    with pytest.raises(preflight.ProducerRuntimePrerequisiteError, match='absolute path'):
        preflight.resolve_runtime_base('relative-runtime')
