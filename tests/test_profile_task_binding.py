"""Task profile identity at real loader/request/durable worker seams; no production."""
from pathlib import Path
import argparse
import hashlib
import json
import os

import pytest

from src.autoslice.channel_profile import (
    ChannelProfileError, PROFILE_BINDING_ENV, binding_for_profile, load_channel_profile,
    profile_environment, require_profile_binding,
)
from src.autoslice import overflow_worker as worker
from src.autoslice import producer_request

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = (ROOT / 'profiles/lidousha/profile.json').read_bytes()


@pytest.fixture(autouse=True)
def isolated_profile_selectors(monkeypatch):
    from src.autoslice import native_audio_runtime
    monkeypatch.setattr(native_audio_runtime, "native_audio_readiness", lambda _root: [{"ready": True}])
    for name in ("AUTOSLICE_PROFILE", "AUTOSLICE_PROFILE_MANIFEST", "AUTOSLICE_PROFILE_BINDING",
                 "AUTOSLICE_PROFILE_MANIFEST_SNAPSHOT"):
        monkeypatch.setenv(name, os.environ.get(name, ''))
        monkeypatch.delenv(name, raising=False)


def selected(root, *, other=False, external=False):
    path = root / ('external.json' if external else 'repo/profiles/lidousha/profile.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    document = json.loads(DEFAULT)
    if other:
        document['profile_id'] = 'audit-other'
        document['identity']['display_name'] = 'ISOLATED_TEST'
    path.write_bytes(json.dumps(document).encode() if other else DEFAULT)
    return load_channel_profile(root / 'repo', manifest_path=path, environ={})


def request_fixture(tmp_path, *, external=False):
    root = tmp_path / 'runtime'
    profile = selected(root, external=external)
    repo = root / 'repo'; repo.mkdir(exist_ok=True)
    (repo / 'DEPLOYED_COMMIT').write_text('a' * 40)
    (repo / 'DEPLOYED_AUTHORITY_MANIFEST.json').write_text('{}')
    from src.autoslice.overflow_release import seal
    release = seal(repo)
    request = {'schema_version': worker.REQUEST_SCHEMA, 'attempt_id': worker.new_attempt_id(),
        'release_sha256': release, 'input_sha256': 'b' * 64, 'lane': 'talk', 'date': '2026-10-13',
        'item': {'cid': 'binding-test'}, 'runtime_root': str(root), 'controller_root': str(root),
        'estimated_peak_bytes': 1, 'operational_reserve_bytes': 0,
        'profile_binding': binding_for_profile(profile)}
    with worker._slot(tmp_path / 'control'):
        request['worker_store_id'] = worker._store_id(tmp_path / 'control', initialize=True)
    request['job_id'] = worker.compute_job_id(request)
    return root, profile, request


def submit(tmp_path, root, request):
    held = []
    class Process:
        pid = 999999999
    def spawn(*args, **kwargs):
        held.append(os.dup(kwargs['pass_fds'][1]))
        return Process()
    try:
        return worker.submit_request(request, worker_root=tmp_path / 'control',
            allowed_runtime_parent=tmp_path, expected_runtime_root=root, popen_factory=spawn)
    finally:
        for fd in held:
            os.close(fd)


def test_bound_loader_ignores_ambient_and_preserves_explicit_alternate(tmp_path):
    a = selected(tmp_path / 'a')
    b = selected(tmp_path / 'b', other=True, external=True)
    ambient = profile_environment(b, {})
    bound = load_channel_profile(a.repo_root, environ=ambient, profile_binding=binding_for_profile(a))
    assert binding_for_profile(bound) == binding_for_profile(a)
    assert load_channel_profile(b.repo_root, environ=ambient).profile_id == 'audit-other'
    with pytest.raises(ChannelProfileError, match='conflicts'):
        load_channel_profile(a.repo_root, profile_id='audit-other', profile_binding=binding_for_profile(a))


def test_same_id_changed_manifest_is_not_old_binding(tmp_path):
    profile = selected(tmp_path, external=True)
    binding = binding_for_profile(profile)
    profile.manifest_path.write_bytes(DEFAULT + b' ')
    with pytest.raises(ChannelProfileError, match='bytes differ'):
        load_channel_profile(profile.repo_root, profile_binding=binding)
    with pytest.raises(ChannelProfileError, match='bytes drifted'):
        binding_for_profile(profile)


def args(path):
    return argparse.Namespace(spec=path, ssh_host='localhost', speaker_mode='uniform_host',
        subtitle_text_overrides=None, subtitle_regression=None, speaker_overrides=None)


def test_producer_missing_or_conflicting_binding_fails_before_output(tmp_path, monkeypatch):
    profile = selected(tmp_path / 'a')
    other = selected(tmp_path / 'b', other=True, external=True)
    path = tmp_path / 'spec.json'
    spec = {'candidate_id': 'binding-test', 'output_root': str(tmp_path / 'output'), 'pieces': []}
    path.write_text(json.dumps(spec))
    seen = []
    monkeypatch.setattr(producer_request, 'require_branding_intro', lambda *a, **k: seen.append('branding'))
    with pytest.raises(producer_request.ProducerRuntimePrerequisiteError, match='binding is missing'):
        producer_request.load_producer_request(args(path), repo_root=profile.repo_root,
            profile_asset_file=profile.asset_file, selected_channel_profile=profile)
    spec['profile_binding'] = binding_for_profile(other); path.write_text(json.dumps(spec))
    with pytest.raises(producer_request.ProducerRuntimePrerequisiteError, match='differs'):
        producer_request.load_producer_request(args(path), repo_root=profile.repo_root,
            profile_asset_file=profile.asset_file, selected_channel_profile=profile,
            trusted_parent_profile_binding=binding_for_profile(profile))
    assert seen == [] and not (tmp_path / 'output').exists()


def test_producer_positive_parent_authority_keeps_input_bytes(tmp_path, monkeypatch):
    profile = load_channel_profile(ROOT, environ={})
    path = tmp_path / 'spec.json'
    path.write_text(json.dumps({'candidate_id': 'binding-test', 'output_root': str(tmp_path / 'out'), 'pieces': []}))
    original = path.read_bytes()
    monkeypatch.setenv('AUTOSLICE_HUMAN_TRUTH_MODE', 'delivery')
    monkeypatch.setattr(producer_request, 'require_branding_intro', lambda *a, **k: None)
    result = producer_request.load_producer_request(args(path), repo_root=ROOT,
        profile_asset_file=profile.asset_file, selected_channel_profile=profile,
        trusted_parent_profile_binding=binding_for_profile(profile))
    assert result.spec['profile_binding'] == result.profile_binding == binding_for_profile(profile)
    assert path.read_bytes() == original


def test_worker_pins_before_executor_and_completed_duplicate_never_restarts(tmp_path, monkeypatch):
    root, profile, request = request_fixture(tmp_path)
    submit(tmp_path, root, request)
    other = selected(tmp_path / 'other', other=True, external=True)
    for name, value in profile_environment(other, {}).items(): monkeypatch.setenv(name, value)
    seen = []
    def execute(req, jobdir):
        loaded = load_channel_profile(root / 'repo')
        seen.append(binding_for_profile(loaded))
        return {'status': 'candidate_rejected', 'candidate_id': 'binding-test', 'profile_binding': binding_for_profile(loaded)}
    result = worker.run_job(worker_root=tmp_path / 'control', job_id=request['job_id'],
        attempt_id=request['attempt_id'], allowed_runtime_parent=tmp_path, executor=execute)
    assert result['status'] == 'completed' and seen == [request['profile_binding']]
    stored = Path(tmp_path / 'control/jobs' / request['job_id'] / request['attempt_id'] / 'result.json').read_bytes()
    duplicate = submit(tmp_path, root, request)
    assert duplicate['duplicate'] and not duplicate['accepted']
    assert Path(duplicate['jobdir'], 'result.json').read_bytes() == stored


def test_manifest_drift_fails_before_executor_then_same_attempt_recovers(tmp_path):
    root, profile, request = request_fixture(tmp_path, external=True)
    first = submit(tmp_path, root, request)
    original = Path(first['jobdir'], 'request.json').read_bytes()
    profile.manifest_path.write_bytes(DEFAULT + b' ')
    seen = []
    result = worker.run_job(worker_root=tmp_path / 'control', job_id=request['job_id'],
        attempt_id=request['attempt_id'], allowed_runtime_parent=tmp_path, executor=lambda *a: seen.append(True))
    assert result['error_code'] == 'runtime_prerequisite_failed' and seen == []
    state = json.loads(Path(first['jobdir'], 'state.json').read_text())
    assert state['startup_stage'] == 'pre_executor' and not Path(first['jobdir'], 'result.json').exists()
    profile.manifest_path.write_bytes(DEFAULT)
    resumed = submit(tmp_path, root, request)
    assert resumed['accepted'] and resumed['attempt_id'] == first['attempt_id']
    assert Path(first['jobdir'], 'request.json').read_bytes() == original


def test_legacy_status_remains_readonly_and_cannot_submit_or_run(tmp_path):
    root, profile, request = request_fixture(tmp_path)
    request.pop('profile_binding'); request['schema_version'] = worker.LEGACY_REQUEST_SCHEMA
    request['job_id'] = worker.compute_job_id(request)
    jobdir = tmp_path / 'control/jobs' / request['job_id'] / request['attempt_id']; jobdir.mkdir(parents=True)
    worker._write(jobdir / 'request.json', request)
    worker._write(jobdir / 'state.json', {'schema_version': worker.STATE_SCHEMA, **{name: request[name] for name in
        ('job_id','attempt_id','release_sha256','input_sha256')}, 'request_sha256': worker.request_sha256(request),
        'status': 'failed', 'pid': None, 'revision': 1, 'error_code': 'runtime_prerequisite_failed', 'startup_stage': 'pre_executor'})
    before = {p.name:p.read_bytes() for p in jobdir.iterdir()}
    status = worker.status_job(worker_root=tmp_path / 'control', job_id=request['job_id'], attempt_id=request['attempt_id'], allowed_runtime_parent=tmp_path)
    assert status['status'] == 'failed'
    with pytest.raises(worker.RequestValidationError): submit(tmp_path, root, request)
    with pytest.raises(worker.RequestValidationError):
        worker.run_job(worker_root=tmp_path / 'control', job_id=request['job_id'], attempt_id=request['attempt_id'], allowed_runtime_parent=tmp_path)
    assert {p.name:p.read_bytes() for p in jobdir.iterdir()} == before


def test_legacy_completed_fetch_is_readonly_not_execution_authority(tmp_path):
    root, _profile, request = request_fixture(tmp_path)
    request.pop("profile_binding"); request["schema_version"] = worker.LEGACY_REQUEST_SCHEMA
    request["job_id"] = worker.compute_job_id(request)
    jobdir = tmp_path / "control/jobs" / request["job_id"] / request["attempt_id"]; jobdir.mkdir(parents=True)
    worker._write(jobdir / "request.json", request)
    worker._write(jobdir / "result.json", {"status": "candidate_rejected", "candidate_id": "binding-test"})
    digest, size = worker._result(jobdir)
    worker._write(jobdir / "state.json", {"schema_version": worker.STATE_SCHEMA,
        **{name: request[name] for name in ("job_id", "attempt_id", "release_sha256", "input_sha256")},
        "request_sha256": worker.request_sha256(request), "status": "completed", "pid": None, "revision": 2,
        "result_sha256": digest, "result_bytes": size, "bundle_path": None, "bundle_bytes": None})
    before = {p.name: p.read_bytes() for p in jobdir.iterdir()}
    description = worker.fetch_description(worker_root=tmp_path / "control", job_id=request["job_id"],
        attempt_id=request["attempt_id"], allowed_runtime_parent=tmp_path)
    assert description["result_sha256"] == digest
    with pytest.raises(worker.RequestValidationError, match="legacy request cannot execute"):
        worker.run_job(worker_root=tmp_path / "control", job_id=request["job_id"], attempt_id=request["attempt_id"], allowed_runtime_parent=tmp_path)
    assert {p.name: p.read_bytes() for p in jobdir.iterdir()} == before
