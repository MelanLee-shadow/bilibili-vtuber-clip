"""Parent/current selections must not replace frozen task or receipt identity."""
import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from scripts.init_channel_profile import init_profile
from src.autoslice import channel_profile as cp, execution_backend as backend, producer_request as producer
from src.autoslice.profile_manifest_history import archive_manifest
# Load source-root-scoped framework dependencies before selecting a profile
# in a separate temporary runtime. Production code and profiles share a root;
# this test deliberately keeps the source tree immutable across new channels.
from src.autoslice.package_import import atomic_write_bytes  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    # Backends and the request loader write some selectors directly.
    for key in (*os.environ, cp.PROFILE_ENV, cp.PROFILE_MANIFEST_ENV,
                cp.PROFILE_BINDING_ENV, cp.PROFILE_SNAPSHOT_ENV,
                'AUTOSLICE_TERM_AS_OF', 'LIDOUSHA_TERM_AS_OF'):
        monkeypatch.setenv(key, os.environ.get(key, ''))
    for key in (cp.PROFILE_ENV, cp.PROFILE_MANIFEST_ENV, cp.PROFILE_BINDING_ENV, cp.PROFILE_SNAPSHOT_ENV):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('AUTOSLICE_HUMAN_TRUTH_MODE', 'delivery')


@pytest.fixture(params=['parent-direction-a', 'parent-direction-b'])
def selections(tmp_path, request):
    runtime = tmp_path / 'runtime'
    repo = runtime / 'repo'
    shutil.copytree(ROOT / 'assets/_template', repo / 'assets/_template')
    shutil.copytree(ROOT / 'profiles/_template', repo / 'profiles/_template')
    (repo / 'DEPLOYED_COMMIT').write_text('a' * 40)
    (repo / 'DEPLOYED_AUTHORITY_MANIFEST.json').write_text('{}')
    init_profile(request.param, display_name='Parent Direction', room='345678', repo_root=repo)
    manifest = repo / 'profiles' / request.param / 'profile.json'
    document = json.loads(manifest.read_text())
    document['assets']['files']['seed_asset'] = 'seed.json'
    manifest.write_text(json.dumps(document))
    archive_manifest(repo, manifest)
    old = cp.binding_for_profile(cp.load_channel_profile(repo, manifest_path=manifest, environ={}))
    document['assets']['files']['future_asset'] = 'future.json'
    document['assets']['directories']['future_directory'] = 'future'
    manifest.write_text(json.dumps(document))
    archive_manifest(repo, manifest)
    current = cp.load_channel_profile(repo, manifest_path=manifest, environ={})
    return runtime, old, current


def load_request(tmp_path, monkeypatch, current, task, parent):
    path = tmp_path / 'spec.json'
    spec = {'candidate_id': 'parent-direction', 'output_root': str(tmp_path / 'out'), 'pieces': []}
    if task is not None:
        spec['profile_binding'] = task
    path.write_text(json.dumps(spec))
    original = path.read_bytes()
    monkeypatch.setattr(producer, 'require_branding_intro', lambda *a, **k: None)
    monkeypatch.setattr(producer, 'validate_producer_runtime_prerequisites', lambda **k: None)
    args = argparse.Namespace(spec=path, ssh_host='localhost', speaker_mode='uniform_host',
        subtitle_text_overrides=None, subtitle_regression=None, speaker_overrides=None)
    result = producer.load_producer_request(args, repo_root=current.repo_root,
        profile_asset_file=current.asset_file, selected_channel_profile=current,
        trusted_parent_profile_binding=parent)
    assert path.read_bytes() == original
    return result


def test_old_spec_new_parent_through_request_loader(selections, tmp_path, monkeypatch, caplog):
    runtime, frozen, current = selections
    parent = cp.binding_for_profile(current)
    for key, value in cp.profile_environment(current, {}).items():
        monkeypatch.setenv(key, value)
    request = load_request(tmp_path, monkeypatch, current, frozen, parent)
    assert request.profile_binding == request.spec['profile_binding'] == frozen
    proof = request.spec['profile_binding_extensions']
    assert proof[0]['old_sha256'] == frozen['manifest_sha256']
    assert proof[0]['new_sha256'] == current.manifest_sha256
    assert proof[0]['added_asset_keys'] == {'files': ['future_asset'], 'directories': ['future_directory']}
    assert json.loads((request.out_root / 'profile_binding_extensions.json').read_text())['profile_binding'] == frozen
    assert 'PROFILE_BINDING_MONOTONIC_EXTENSION' in caplog.text


def test_backend_current_parent_keeps_old_task_and_live_selection(selections, monkeypatch, caplog):
    runtime, frozen, current = selections
    for key, value in cp.profile_environment(current, {}).items():
        monkeypatch.setenv(key, value)
    from src.autoslice.overflow_release import seal
    release = seal(current.repo_root)
    instance = backend.SSHBackend(runtime, {'release_sha256': release, 'profile_binding': frozen,
        'nodes': [{'host': 'isolated-test', 'runtime_root': str(runtime), 'worker_root': '/unused'}]}, log=lambda _: None)
    instance._verify_live_profile()
    assert instance.profile_binding == frozen
    assert instance.profile.binding_extension is not None
    assert 'PROFILE_BINDING_MONOTONIC_EXTENSION' in caplog.text


@pytest.mark.parametrize('mode', ['equal', 'legacy'])
def test_equal_and_parent_authorized_legacy_spec(selections, tmp_path, monkeypatch, mode):
    _, _, current = selections
    binding = cp.binding_for_profile(current)
    result = load_request(tmp_path, monkeypatch, current, binding if mode == 'equal' else None, binding)
    assert result.profile_binding == binding
    assert 'profile_binding_extensions' not in result.spec


@pytest.mark.parametrize('mutation', ['older', 'delete', 'value', 'profile_id', 'path'])
def test_request_and_backend_refuse_reverse_or_non_extension(selections, tmp_path, monkeypatch, mutation):
    runtime, frozen, current = selections
    task, parent = dict(frozen), cp.binding_for_profile(current)
    if mutation == 'older':
        task, parent = parent, task
    elif mutation in {'delete', 'value'}:
        document = json.loads(current.manifest_path.read_text())
        if mutation == 'delete':
            del document['assets']['files']['seed_asset']
        else:
            document['assets']['files']['seed_asset'] = 'different.json'
        current.manifest_path.write_text(json.dumps(document))
        parent['manifest_sha256'] = hashlib.sha256(current.manifest_path.read_bytes()).hexdigest()
        archive_manifest(current.repo_root, current.manifest_path)
    else:
        parent['profile_id' if mutation == 'profile_id' else 'manifest_path'] = (
            'different-channel' if mutation == 'profile_id' else 'profiles/elsewhere/profile.json')
    with pytest.raises(producer.ProducerRuntimePrerequisiteError, match='channel_profile.*differs'):
        load_request(tmp_path, monkeypatch, current, task, parent)
    assert not (tmp_path / 'out').exists()
    monkeypatch.setenv(cp.PROFILE_BINDING_ENV, json.dumps(parent))
    with pytest.raises(cp.ChannelProfileError, match='differs'):
        backend._capture_profile(runtime, task)


@pytest.mark.parametrize('where', ['result', 'summary'])
def test_old_record_cannot_be_rebound_to_new_task(selections, where):
    _, frozen, current = selections
    record = {'profile_binding': frozen} if where == 'result' else {'summary': {'profile_binding': frozen}}
    with pytest.raises(cp.ChannelProfileError, match='differs'):
        cp.bind_profile_result(record, current)
