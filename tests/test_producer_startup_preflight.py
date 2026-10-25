from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

from src.autoslice.channel_profile import binding_for_profile, load_channel_profile

from src.autoslice import producer_request as preflight
from src.autoslice import producer_speaker


def _tools(monkeypatch, missing=()):
    monkeypatch.setattr(preflight.shutil, 'which', lambda name: None if name in missing else sys.executable)


@pytest.mark.parametrize('command', ['ffmpeg', 'ffprobe'])
def test_missing_media_executable_is_typed_before_request_output(tmp_path, monkeypatch, command):
    _tools(monkeypatch, {command})
    monkeypatch.setattr(preflight, 'require_branding_intro', lambda *a, **k: None)
    spec = tmp_path / 'spec.json'
    output = tmp_path / 'output'
    spec.write_text(json.dumps({'candidate_id': 'preflight', 'output_root': str(output), 'pieces': []}))
    args = argparse.Namespace(spec=spec, ssh_host='localhost', speaker_mode='uniform_host',
        subtitle_text_overrides=None, subtitle_regression=None, speaker_overrides=None)
    with pytest.raises(preflight.ProducerRuntimePrerequisiteError, match=command):
        preflight.load_producer_request(args, repo_root=Path(__file__).resolve().parents[1],
            profile_asset_file=lambda key: tmp_path / 'unused',
            trusted_parent_profile_binding=binding_for_profile(load_channel_profile(Path(__file__).resolve().parents[1], environ={})))
    assert not output.exists()


def test_required_speaker_checks_selected_executable(monkeypatch):
    _tools(monkeypatch, {'/missing/speaker-python'})
    with pytest.raises(preflight.ProducerRuntimePrerequisiteError, match='speaker_python'):
        preflight.validate_producer_runtime_prerequisites(speaker_mode='required', speaker_python='/missing/speaker-python')


@pytest.mark.parametrize('mode', ['uniform_host', 'auto'])
def test_unused_binary_speaker_and_agy_do_not_block_local_branch(monkeypatch, mode):
    _tools(monkeypatch, {'agy', 'ssh', 'scp', '/missing/speaker-python'})
    preflight.validate_producer_runtime_prerequisites(speaker_mode=mode, speaker_python='/missing/speaker-python')


def test_remote_interpreter_is_not_checked_on_controller(monkeypatch):
    _tools(monkeypatch, {'/remote/speaker-python'})
    preflight.validate_producer_runtime_prerequisites(host='remote-media', speaker_mode='required', speaker_python='/remote/speaker-python')
    _tools(monkeypatch, {'ssh'})
    with pytest.raises(preflight.ProducerRuntimePrerequisiteError, match='ssh'):
        preflight.validate_producer_runtime_prerequisites(host='remote-media')


def test_song_checks_its_selected_host_vocal_python(monkeypatch):
    _tools(monkeypatch, {'/missing/vocal-python'})
    preflight.validate_producer_runtime_prerequisites(speaker_mode='uniform_host', host_vocal_python='/missing/vocal-python')
    with pytest.raises(preflight.ProducerRuntimePrerequisiteError, match='host_vocal_python'):
        preflight.validate_producer_runtime_prerequisites(lane='song', host_vocal_python='/missing/vocal-python')


def test_relative_selected_interpreter_is_not_reinterpreted_by_cwd(monkeypatch):
    _tools(monkeypatch)
    with pytest.raises(preflight.ProducerRuntimePrerequisiteError, match='absolute path'):
        preflight.validate_producer_runtime_prerequisites(speaker_mode='required', speaker_python='venv/bin/python')


def test_cli_default_speaker_python_uses_configured_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    monkeypatch.delenv('AUTOSLICE_SPEAKER_PYTHON', raising=False)
    args = preflight.parse_producer_args(['--spec', 'unused.json'], description='', speaker_display_name='host', default_speaker_mode='uniform_host')
    assert args.speaker_python == tmp_path / 'venv-diar/bin/python'


@pytest.mark.parametrize('error', [FileNotFoundError('disappeared'), PermissionError('not executable')])
def test_speaker_launch_race_is_typed_and_stale_outputs_cleared(tmp_path, monkeypatch, error):
    _tools(monkeypatch)
    paths = {name: tmp_path / name for name in ['media', 'text', 'profile', 'out-srt', 'out-ass', 'out-json']}
    for path in paths.values():
        path.write_bytes(b'fixture')
    def launch(*args, **kwargs):
        raise error
    monkeypatch.setattr(producer_speaker.subprocess, 'run', launch)
    with pytest.raises(preflight.ProducerRuntimePrerequisiteError, match='speaker_python.*launch failed'):
        producer_speaker.run_speaker_finalizer(host='localhost', candidate_id='preflight',
            media_path=paths['media'], text_srt_path=paths['text'], output_srt_path=paths['out-srt'],
            output_ass_path=paths['out-ass'], output_manifest_path=paths['out-json'],
            work_dir=tmp_path / 'work', speaker_python=Path(sys.executable), profile_path=paths['profile'])
    assert not any(paths[key].exists() for key in ['out-srt', 'out-ass', 'out-json'])
