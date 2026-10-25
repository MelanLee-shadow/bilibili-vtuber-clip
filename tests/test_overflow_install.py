import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import time
import sys
import fcntl
import shlex

import pytest

from src.autoslice import overflow_install as install


def test_native_configuration_uses_only_protected_ssh_stdin_and_actual_worker_loader(tmp_path, monkeypatch):
    controller=tmp_path/'controller';controller.mkdir()
    (controller/'mai.env').write_text('AZURE_ENDPOINT=https://resource.services.ai.azure.com\nAZURE_API_KEY=test-mai\nCPA_API_KEY=not-an-audio-key\n')
    (controller/'moss.env').write_text('MOSS_API_KEY=test-moss\nAZURE_API_KEY=not-moss\n')
    for p in controller.glob('*.env'):p.chmod(0o600)
    real_run=subprocess.run;seen=[]
    def ssh_transport(command, **kwargs):
        assert 'test-mai' not in repr(command) and 'test-moss' not in repr(command)
        payload=json.loads(kwargs['input']);assert set(payload)=={'mai','moss'}
        assert set(payload['mai'])=={'AZURE_ENDPOINT','AZURE_API_KEY'}
        assert set(payload['moss'])=={'AUTOSLICE_MOSS_API_KEY'}
        args=shlex.split(command[-1]);root=tmp_path/command[-2];root.mkdir(exist_ok=True)
        response=real_run([sys.executable,'-B','-c',args[5],str(root)],**kwargs)
        assert response.returncode==0,response.stderr
        result=json.loads(response.stdout);result['runtime_root']=str(controller);seen.append(root)
        return subprocess.CompletedProcess(command,0,stdout=json.dumps(result).encode())
    monkeypatch.setattr(install.subprocess,'run',ssh_transport)
    nodes=[{'host':h,'runtime_root':str(controller)} for h in ('worker1','worker2')]
    result=install._sync_native_audio_nodes(controller,nodes)
    assert len(result)==2 and len(seen)==2
    assert all(p.joinpath('mai.env').stat().st_mode&0o777==0o600 for p in seen)
    assert 'test-mai' not in json.dumps(result) and 'test-moss' not in json.dumps(result)


def test_native_only_sync_preserves_active_jobs_code_and_registration(tmp_path, monkeypatch):
    runtime=tmp_path/'runtime';runtime.mkdir();config=runtime/'execution-backends.json'
    config.write_text(json.dumps({'mode':'remote','nodes':[{'host':'worker'}]}));before=config.read_bytes()
    monkeypatch.setattr(install,'verify_deployed',lambda _:None)
    monkeypatch.setattr(install,'_sync_native_audio_nodes',lambda *_:[{'status':'READY'}])
    monkeypatch.setattr(install,'install_worker',lambda *_:pytest.fail('must not switch active code'))
    result=install.sync_native_audio(runtime,config)
    assert result['status']=='NATIVE_AUDIO_READY' and result['code_switched'] is False
    assert config.read_bytes()==before and not (runtime/'feature-overlays').exists()


def test_audio_sync_rejects_unready_remote_without_echoing_credentials(tmp_path, monkeypatch):
    from src.autoslice import native_audio_runtime as audio
    monkeypatch.setattr(audio,'validated_native_audio_values',lambda provider,**_: {'key':'private-test-key'})
    monkeypatch.setattr(install.subprocess,'run',lambda command,**_:subprocess.CompletedProcess(
        command,1,stdout=b'{"status":"NOT_READY"}',stderr=b'private-test-key'))
    with pytest.raises(ValueError) as caught:
        install._sync_native_audio_nodes(tmp_path,[{'host':'worker','runtime_root':str(tmp_path)}])
    assert 'private-test-key' not in str(caught.value)


def repository(root, commit='1' * 40, script=b'old code\n', *, with_package=False):
    root.mkdir(parents=True)
    for tree in ('src', 'scripts', 'ops', 'assets', 'profiles'):
        (root / tree).mkdir()
    (root / 'src/autoslice').mkdir()
    shutil.copyfile(Path(install.__file__).with_name('overflow_release.py'),
                    root / 'src/autoslice/overflow_release.py')
    shutil.copyfile(Path(install.__file__).with_name('evidence_identity.py'),
                    root / 'src/autoslice/evidence_identity.py')
    if with_package:
        source = Path(install.__file__).parents[2]
        for name in ('src/__init__.py', 'src/autoslice/__init__.py',
                     'src/autoslice/runtime_code_lease.py'):
            shutil.copyfile(source / name, root / name)
        shutil.copyfile(source / 'src/autoslice/runtime_code_lease.py',
                        root.parent / 'runtime-code-lease.py')
    (root / 'scripts/program.py').write_bytes(script)
    entries = {}
    for p in sorted(root.rglob('*')):
        row = {'type': 'dir' if p.is_dir() else 'file', 'mode': p.stat().st_mode & 0o777}
        if p.is_file():
            row['sha256'] = install.digest(p.read_bytes())
        entries[str(p.relative_to(root))] = row
    (root / 'DEPLOYED_COMMIT').write_text(commit + '\n')
    (root / 'DEPLOYED_MANIFEST.json').write_text(json.dumps({
        'schema_version': 'oci3-shadow-autoslice-repo-manifest.v1',
        'commit': commit, 'entries': entries, 'tree_sha256': install.digest(json.dumps(
            entries, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())}))
    authority = {'schema_version': 'deployed-authority-manifest.v1', 'deployed_commit': commit,
                 'entries': {'scripts/program.py': {'bytes': len(script), 'sha256': 'sha256:' + install.digest(script)}}}
    authority['manifest_sha256'] = 'sha256:' + install.digest(json.dumps(
        authority, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())
    (root / 'DEPLOYED_AUTHORITY_MANIFEST.json').write_text(json.dumps(authority))
    return root


def test_stdin_installer_loads_release_from_verified_repo_without_inherited_path(tmp_path):
    repo = repository(tmp_path / 'versions' / ('1' * 40) / 'repo', with_package=True)
    foreign = tmp_path / 'foreign'
    foreign.mkdir()
    # The reported transport executes installer source through python -c,
    # before any src package has been imported. Exercise that exact boundary.
    source = Path(install.__file__).read_text()
    probe = source + '''
import sys
repo = Path(sys.argv[1])
before = sys.path[:]
manifest, release, actual = verify_deployed(repo)
assert sys.path == before
loaded = Path(release['inventory'].__code__.co_filename).resolve()
assert loaded == (repo / 'src/autoslice/overflow_release.py').resolve(), loaded
assert 'src/autoslice/overflow_release.py' in actual
assert manifest['commit'] == '1' * 40
'''
    environment = {key: value for key, value in os.environ.items()
                   if key not in {'PYTHONPATH', 'PYTHONHOME'}}
    result = subprocess.run([sys.executable, '-B', '-c', probe, str(repo)],
                            cwd=foreign, env=environment, capture_output=True,
                            text=True, timeout=60)
    assert result.returncode == 0, result.stderr


def worker(tmp_path):
    runtime = tmp_path / 'worker'
    repository(runtime / 'repo')
    (runtime / 'README.md').write_text('prepare-only role')
    (runtime / 'AGENTS.md').write_text('controller owns publication')
    store = tmp_path / 'store'
    (store / 'jobs').mkdir(parents=True)
    return runtime, store


def candidate(tmp_path):
    repo = repository(tmp_path / 'source', '2' * 40, b'new code\n')
    return install.snapshot_archive(repo)


def test_private_package_provenance_replays_complete_original_deployment_without_current_commit_permission(tmp_path,monkeypatch):
    from src.autoslice.producer_delivery_transaction import deployment_authority_binding
    from src.autoslice.normal_first_publication_media_successor import _require_prepared_deployment, NormalFirstPublicationMediaSuccessorError
    monkeypatch.delenv('AUTOSLICE_RUNTIME_VERSION_ROOT',raising=False)
    runtime=tmp_path/'runtime';runtime.mkdir()
    original=repository(runtime/'repo','a'*40)
    bound=deployment_authority_binding(runtime)
    archived=runtime/'versions'/('a'*40)/'repo';archived.parent.mkdir(parents=True)
    original.rename(archived)
    repository(runtime/'repo','b'*40,b'new deployment\n')
    document={'deployed_authority':bound}
    with pytest.raises(NormalFirstPublicationMediaSuccessorError,match='drifts from current'):
        _require_prepared_deployment(document,runtime)
    _require_prepared_deployment(document,runtime,frozen_private_provenance=True)
    (archived/'scripts/program.py').write_bytes(b'tampered original asset')
    with pytest.raises(NormalFirstPublicationMediaSuccessorError,match='missing or changed'):
        _require_prepared_deployment(document,runtime,frozen_private_provenance=True)


def test_capture_requires_actual_deployed_bytes(tmp_path):
    repo = repository(tmp_path / 'repo')
    (repo / 'scripts/program.py').write_text('drift')
    with pytest.raises(ValueError, match='deployed file differs'):
        install.snapshot_archive(repo)


def test_unmanifested_code_cannot_be_sealed(tmp_path):
    repo = repository(tmp_path / 'repo')
    (repo / 'scripts/untracked.py').write_text('unexpected')
    with pytest.raises(ValueError, match='unmanifested'):
        install.snapshot_archive(repo)


@pytest.mark.parametrize('name,type_', [('../escape', tarfile.REGTYPE),
                                      ('scripts/link', tarfile.SYMTYPE)])
def test_extract_rejects_escaping_or_link_members(tmp_path, name, type_):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as tar:
        member = tarfile.TarInfo(name)
        member.type = type_
        tar.addfile(member)
    buffer.seek(0)
    with pytest.raises(ValueError):
        install.extract(buffer, tmp_path / 'stage')
    assert not (tmp_path / 'escape').exists()


def test_idle_install_preserves_old_repo_and_role(tmp_path, monkeypatch):
    runtime, store = worker(tmp_path)
    archive, seal, _ = candidate(tmp_path)
    calls = []
    def startup(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(install.subprocess, 'run', startup)
    result = install.install_worker(io.BytesIO(archive), runtime, store, seal)
    assert result['status'] == 'INSTALLED'
    assert (Path(result['preimage_path']) / 'scripts/program.py').read_bytes() == b'old code\n'
    assert (runtime / 'repo/scripts/program.py').read_bytes() == b'new code\n'
    assert (runtime / 'README.md').read_text() == 'prepare-only role'
    assert calls and 'validate_runtime_prerequisites' in calls[0][3]
    again = install.install_worker(io.BytesIO(archive), runtime, store, seal)
    assert again['status'] == 'ALREADY_INSTALLED'
    assert len(calls) == 1


def test_startup_failure_restores_exact_old_repo(tmp_path, monkeypatch):
    runtime, store = worker(tmp_path)
    archive, seal, _ = candidate(tmp_path)
    before = install.snapshot_archive(runtime / 'repo')[1]
    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, stderr=b'prerequisite missing')
    monkeypatch.setattr(install.subprocess, 'run', fail)
    with pytest.raises(subprocess.CalledProcessError):
        install.install_worker(io.BytesIO(archive), runtime, store, seal)
    assert install.snapshot_archive(runtime / 'repo')[1] == before
    assert list((runtime / 'feature-overlays').glob('*/repo.rejected'))


@pytest.mark.parametrize('status', ['unknown', 'running', 'completed'])
def test_active_or_unknown_store_blocks_replacement(tmp_path, status):
    pid = os.getpid() if status == 'completed' else None
    runtime, store = worker(tmp_path)
    attempt = store / 'jobs' / ('a' * 64) / ('b' * 32)
    attempt.mkdir(parents=True)
    (attempt / 'state.json').write_text(json.dumps({'status': status, 'pid': pid}))
    archive, seal, _ = candidate(tmp_path)
    with pytest.raises(ValueError):
        install.install_worker(io.BytesIO(archive), runtime, store, seal)
    assert (runtime / 'repo/scripts/program.py').read_bytes() == b'old code\n'


def test_second_worker_failure_does_not_publish_controller_binding(tmp_path, monkeypatch):
    monkeypatch.setattr(install, '_sync_native_audio_nodes', lambda *_: [])
    runtime = tmp_path / 'controller'
    repository(runtime / 'repo')
    nodes = [{'host': n, 'runtime_root': str(runtime), 'worker_root': '/data/store'}
             for n in ('worker1', 'worker2')]
    config = runtime / 'execution-backends.json'
    config.write_text(json.dumps({'mode': 'auto', 'release_sha256': 'f' * 64, 'nodes': nodes}))
    before = config.read_bytes()
    seal = install.snapshot_archive(runtime / 'repo')[1]
    def rpc(command, **kwargs):
        if 'worker2' in command:
            raise subprocess.CalledProcessError(1, command, stderr=b'busy')
        if command[-1].endswith('probe'):
            value = {'startup_ok': True, 'release_sha256': seal, 'runtime_root': str(runtime),
                     'worker_root': '/data/store', 'worker_store_id': 'c' * 32}
        else:
            value = {'status': 'INSTALLED'}
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(value).encode())
    monkeypatch.setattr(install.subprocess, 'run', rpc)
    with pytest.raises(subprocess.CalledProcessError):
        install.sync(runtime, config)
    assert config.read_bytes() == before
    assert not (runtime / 'repo/OVERFLOW_RELEASE_SHA256').exists()


def test_registration_waits_for_actual_code_readers(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    (runtime / 'repo/src/autoslice').mkdir(parents=True)
    (runtime / 'repo/src/autoslice/runtime_code_lease.py').touch()
    path = runtime / '.oci3-runtime-code.lock'
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_SH)
    monkeypatch.setenv('AUTOSLICE_RUNTIME_CODE_LEASE_FD', str(fd))
    child = subprocess.Popen([sys.executable, '-c',
        "import fcntl,sys,time;h=open(sys.argv[1],'rb');fcntl.flock(h,fcntl.LOCK_SH);print('ready',flush=True);time.sleep(.25)",
        str(path)], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'ready'
        start = time.monotonic()
        with install.registration_window(runtime):
            assert time.monotonic() - start > .1
            assert os.environ['AUTOSLICE_RUNTIME_CODE_LEASE_MODE'] == 'exclusive'
        assert 'AUTOSLICE_RUNTIME_CODE_LEASE_MODE' not in os.environ
    finally:
        child.wait(timeout=3)
        os.close(fd)


def test_configured_adoption_hash_must_match_before_worker_install(tmp_path, monkeypatch):
    runtime = tmp_path / 'controller'
    repository(runtime / 'repo')
    inventory = runtime / 'explicit-inventory.json'
    inventory.write_text('{}')
    config = runtime / 'execution-backends.json'
    config.write_text(json.dumps({'mode': 'auto', 'nodes': [{'host': 'worker',
        'runtime_root': str(runtime), 'worker_root': '/data/store',
        'legacy_adoption_inventory': {'path': str(inventory), 'sha256': '0' * 64}}]}))
    def forbidden(*args, **kwargs):
        pytest.fail('invalid adoption input must be rejected before SSH installation')
    monkeypatch.setattr(install.subprocess, 'run', forbidden)
    with pytest.raises(ValueError, match='adoption declaration differs'):
        install.sync(runtime, config)


def test_versioned_worker_switch_keeps_active_job_and_old_code(tmp_path, monkeypatch):
    runtime, store = worker(tmp_path)
    archive, first, _ = candidate(tmp_path)
    monkeypatch.setattr(install.subprocess, 'run',
                        lambda command, **kwargs: subprocess.CompletedProcess(command, 0))
    install.install_worker(io.BytesIO(archive), runtime, store, first)
    old_repo = (runtime / 'repo').resolve()
    attempt = store / 'jobs' / ('a' * 64) / ('b' * 32)
    attempt.mkdir(parents=True)
    state = attempt / 'state.json'
    state.write_text(json.dumps({'status': 'running', 'pid': os.getpid()}))
    before = state.read_bytes()
    newer = repository(tmp_path / 'next-source', '3' * 40, b'next code\n')
    archive, second, _ = install.snapshot_archive(newer)
    # An admitted old job holds this lock throughout execution. New version
    # publication must neither acquire it nor overwrite its code or state.
    with (store / 'jobs/.worker-slot.lock').open('rb') as slot:
        fcntl.flock(slot, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = install.install_worker(io.BytesIO(archive), runtime, store, second)
    assert result['versioned'] is True
    assert old_repo.joinpath('scripts/program.py').read_bytes() == b'new code\n'
    assert runtime.joinpath('repo/scripts/program.py').read_bytes() == b'next code\n'
    assert runtime.joinpath('repo').resolve() != old_repo
    assert state.read_bytes() == before


def test_release_inventory_loads_on_a_staged_tree_before_its_code_lease_exists(tmp_path):
    """Deploy 22b: a staged worker tree already carries DEPLOYED_COMMIT, but its
    runtime-code-lease.py is written only after the tree is moved into place.
    The release-inventory module is runpy-loaded at that moment, so it must stay
    standalone instead of importing the src package (whose __init__ needs the lease)."""
    import ast
    import runpy

    root = Path(__file__).resolve().parents[1]
    source = root / 'src/autoslice/overflow_release.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    imported |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    assert not any(name == 'src' or name.startswith('src.') for name in imported)

    stage = tmp_path / 'overlay' / 'repo.staged'
    (stage / 'src/autoslice').mkdir(parents=True)
    for name in ('__init__.py', 'overflow_release.py'):
        shutil.copy2(root / 'src/autoslice' / name, stage / 'src/autoslice' / name)
    (stage / 'src/__init__.py').write_text('')
    (stage / 'DEPLOYED_COMMIT').write_text('0' * 40 + '\n')
    (stage / 'DEPLOYED_AUTHORITY_MANIFEST.json').write_text('{}')
    assert not (stage.parent / 'runtime-code-lease.py').exists()
    release = runpy.run_path(str(stage / 'src/autoslice/overflow_release.py'))
    rows = release['inventory'](stage)
    assert 'src/autoslice/overflow_release.py' in rows and 'DEPLOYED_COMMIT' in rows
