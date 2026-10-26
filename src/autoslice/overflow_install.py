"""Install the controller's verified repository on idle execution workers.

Registration is published only after every configured worker passes its normal
startup/store probe. This module also runs as a stdlib-only SSH bootstrap while
the worker repository is being replaced.
"""
from __future__ import annotations

import contextlib
import datetime
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import runpy
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import uuid


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError(f"unsafe installation input: {path}")
    data = path.read_bytes()
    after = path.lstat()
    fields = ('st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_uid', 'st_gid',
              'st_size', 'st_mtime_ns', 'st_ctime_ns')
    if any(getattr(after, name) != getattr(before, name) for name in fields):
        raise ValueError(f"installation input changed: {path}")
    return data


def write(path, data):
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    before = path.lstat() if path.exists() or path.is_symlink() else None
    if before is not None and (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1):
        raise ValueError('unsafe installation output')
    with temporary.open('xb') as handle:
        os.fchmod(handle.fileno(), stat.S_IMODE(before.st_mode) if before else 0o600)
        if before:
            os.fchown(handle.fileno(), before.st_uid, before.st_gid)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()


def relative(raw):
    p = PurePosixPath(raw)
    if not raw or p.is_absolute() or '..' in p.parts or str(p) != raw:
        raise ValueError('unsafe archive member')
    return p


def active_repository(runtime):
    runtime = Path(runtime).resolve()
    own = Path(__file__).resolve().parents[2]
    if own.parent.parent == runtime / 'versions' and (own / 'DEPLOYED_COMMIT').is_file():
        return own
    current = runtime / 'current/repo'
    return current.resolve() if current.is_dir() else runtime / 'repo'


def verify_deployed(repo):
    manifest = json.loads(read(repo / 'DEPLOYED_MANIFEST.json'))
    commit = read(repo / 'DEPLOYED_COMMIT').decode().strip()
    if (manifest.get('schema_version') != 'oci3-shadow-autoslice-repo-manifest.v1'
            or manifest.get('commit') != commit or not re.fullmatch('[0-9a-f]{40}', commit)):
        raise ValueError('deployed repository identity differs')
    entries = manifest['entries']
    canonical_entries = json.dumps(entries, ensure_ascii=False, sort_keys=True,
                                   separators=(',', ':'), allow_nan=False).encode()
    if digest(canonical_entries) != manifest.get('tree_sha256'):
        raise ValueError('deployed manifest digest differs')
    for name, row in entries.items():
        path = repo / str(relative(name))
        mode = path.lstat().st_mode
        if path.is_symlink() or stat.S_IMODE(mode) != row['mode']:
            raise ValueError(f'deployed mode/path differs: {name}')
        if row['type'] == 'dir':
            if not stat.S_ISDIR(mode):
                raise ValueError(f'deployed directory differs: {name}')
        elif row['type'] != 'file' or digest(read(path)) != row['sha256']:
            raise ValueError(f'deployed file differs: {name}')
    authority = json.loads(read(repo / 'DEPLOYED_AUTHORITY_MANIFEST.json'))
    authority_digest = authority.pop('manifest_sha256', None)
    canonical_authority = json.dumps(authority, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()
    if (authority.get('schema_version') != 'deployed-authority-manifest.v1'
            or authority.get('deployed_commit') != commit
            or 'sha256:' + digest(canonical_authority) != authority_digest):
        raise ValueError('deployed authority identity differs')
    for name, row in authority['entries'].items():
        data = read(repo / str(relative(name)))
        if len(data) != row['bytes'] or 'sha256:' + digest(data) != row['sha256']:
            raise ValueError(f'deployed authority file differs: {name}')
    release_path = (repo / 'src/autoslice/overflow_release.py').resolve()
    # run_path locates the file but does not put its repository on sys.path.
    # Bind imports to that same physical version, never cwd or current/repo.
    previous_path = sys.path[:]
    try:
        sys.path.insert(0, str(release_path.parents[2]))
        release = runpy.run_path(str(release_path))
    finally:
        sys.path[:] = previous_path
    actual = release['inventory'](repo)
    allowed = {n for n, v in entries.items() if v['type'] == 'file'}
    allowed |= {'DEPLOYED_COMMIT', 'DEPLOYED_AUTHORITY_MANIFEST.json'}
    if set(actual) - allowed:
        raise ValueError('unmanifested executable/asset files; preserve repository')
    return manifest, release, actual


def snapshot_archive(repo):
    manifest, release, files = verify_deployed(repo)
    raw = json.dumps({'schema_version': 'autoslice-overflow-release.v1', 'files': files},
                     sort_keys=True, separators=(',', ':')).encode()
    seal = digest(raw)
    metadata = {release['MANIFEST']: raw, release['MARKER']: (seal + '\n').encode()}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as archive:
        names = set(manifest['entries']) | {'DEPLOYED_MANIFEST.json', 'DEPLOYED_COMMIT',
                                           'DEPLOYED_AUTHORITY_MANIFEST.json'}
        for name in sorted(names):
            archive.add(repo / name, arcname=name, recursive=False)
        for name, data in metadata.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            archive.addfile(info, io.BytesIO(data))
    # A deployment or source mutation during capture must not produce a seal.
    if verify_deployed(repo)[2] != files:
        raise ValueError('controller changed during overflow snapshot')
    return buffer.getvalue(), seal, metadata


def extract(stream, target):
    seen, total = set(), 0
    with tarfile.open(fileobj=stream, mode='r|*') as archive:
        for member in archive:
            name = member.name.rstrip('/')
            path = target / str(relative(name))
            if name in seen or not (member.isdir() or member.isfile()):
                raise ValueError('duplicate or non-regular archive entry')
            seen.add(name)
            total += member.size
            if total > 1024**3 or len(seen) > 20000:
                raise ValueError('overflow archive exceeds installation bound')
            path.parent.mkdir(parents=True, exist_ok=True)
            if member.isdir():
                path.mkdir(exist_ok=True)
            else:
                with path.open('xb') as out:
                    source = archive.extractfile(member)
                    for chunk in iter(lambda: source.read(1024**2), b''):
                        out.write(chunk)
                    out.flush()
                    os.fsync(out.fileno())
            os.chmod(path, member.mode & 0o777)
    for path in [target, *(p for p in target.rglob('*') if p.is_dir())]:
        fd = os.open(path, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


@contextlib.contextmanager
def lock(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError('unsafe installation lock')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError('installation lock path changed')
        yield fd
    finally:
        os.close(fd)


def jobs_snapshot(root):
    files = {}
    jobs = root / 'jobs'
    for job in jobs.iterdir():
        if job.name == '.worker-slot.lock':
            read(job)
            continue
        if job.is_symlink() or not job.is_dir() or not re.fullmatch('[0-9a-f]{64}', job.name):
            raise ValueError('unknown worker job; preserve runtime')
        for attempt in job.iterdir():
            if attempt.is_symlink() or not attempt.is_dir() or not re.fullmatch('[0-9a-f]{32}', attempt.name):
                raise ValueError('unknown worker attempt; preserve runtime')
            state = json.loads(read(attempt / 'state.json'))
            if state.get('status') not in {'completed', 'failed'}:
                raise ValueError('worker has active/unknown job; preserve runtime')
            pid = state.get('pid')
            if pid is not None:
                if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
                    raise ValueError('invalid worker PID')
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    pass
                else:
                    raise ValueError('worker job PID remains alive; preserve runtime')
            for path in attempt.rglob('*'):
                if path.is_symlink():
                    raise ValueError('unsafe worker store')
                if path.is_file():
                    files[str(path.relative_to(root))] = digest(read(path))
                elif not path.is_dir():
                    raise ValueError('unsafe worker store type')
    return files


def install_worker(stream, runtime, worker_root, expected):
    runtime, worker_root = Path(runtime), Path(worker_root)
    if not re.fullmatch('[0-9a-f]{64}', expected):
        raise ValueError('invalid worker installation release')
    for p in (runtime, worker_root, worker_root / 'jobs'):
        if not p.is_absolute() or p.resolve() != p or not p.is_dir():
            raise ValueError('unsafe/missing worker installation root')
    space = os.statvfs(runtime)
    if space.f_bavail * space.f_frsize < 4 * 1024**3:
        raise ValueError('insufficient worker installation capacity')
    overlays = runtime / 'feature-overlays'
    if overlays.exists() and (not overlays.is_dir() or overlays.resolve() != overlays):
        raise ValueError('unsafe worker feature overlay root')
    overlay = overlays / ('overflow-' + expected + '-' + uuid.uuid4().hex[:8])
    overlay.mkdir(parents=True, mode=0o700)
    stage = overlay / 'repo.staged'
    stage.mkdir()
    extract(stream, stage)
    manifest, release, _ = verify_deployed(stage)
    release['verify'](stage, expected)
    repo = runtime / 'repo'
    versions = runtime / 'versions'
    if versions.exists() and (versions.is_symlink() or not versions.is_dir()):
        raise ValueError('unsafe worker version root')
    version = versions / expected
    target = version / 'repo'
    if repo.is_symlink():
        if not repo.resolve().is_relative_to(versions) or not repo.is_dir():
            raise ValueError('unsafe installed worker repository')
    elif repo.resolve() != repo or not repo.is_dir():
        raise ValueError('unsafe installed worker repository')
    if target.exists():
        if target.resolve() != target:
            raise ValueError('unsafe immutable worker version')
        release['verify'](target, expected)
        verify_deployed(target)
        shutil.rmtree(stage)
        if repo.resolve() == target:
            return {'status': 'ALREADY_INSTALLED', 'release_sha256': expected,
                    'commit': manifest['commit'], 'preimage_path': None, 'versioned': True}
    else:
        version.mkdir(parents=True)
        stage.rename(target)
        wrapper = target / 'src/autoslice/runtime_code_lease.py'
        if wrapper.is_file():
            write(version / 'runtime-code-lease.py', read(wrapper))
    roles = {str(p): digest(read(p)) for p in (runtime / 'README.md', runtime / 'AGENTS.md')}
    legacy = not repo.is_symlink()
    backup = overlay / 'repo.before'
    old_link = os.readlink(repo) if not legacy else None
    bootstrap = runtime / 'runtime-code-lease.py'
    old_bootstrap = read(bootstrap) if bootstrap.exists() else None
    migrated = (target / 'src/autoslice/runtime_code_lease.py').is_file()
    with contextlib.ExitStack() as stack:
        # The initial legacy migration needs idle readers. Once versioned,
        # only the alias switches; old jobs retain their physical script path.
        if legacy:
            if migrated:
                stack.enter_context(lock(runtime / '.oci3-runtime-code.lock'))
            stack.enter_context(lock(worker_root / 'jobs/.worker-slot.lock'))
            before = jobs_snapshot(worker_root)
        else:
            before = {}
        stack.enter_context(lock(runtime / '.runtime-version-switch.lock'))
        env = os.environ.copy()
        for name in ('AUTOSLICE_RUNTIME_VERSION_ROOT', 'AUTOSLICE_RUNTIME_CODE_LEASE_FD',
                     'AUTOSLICE_RUNTIME_CODE_LEASE_MODE'):
            env.pop(name, None)
        command = "import sys;sys.path.insert(0,sys.argv[3]);from src.autoslice.overflow_worker import validate_runtime_prerequisites;validate_runtime_prerequisites(runtime_root=sys.argv[1],expected_release=sys.argv[2])"
        try:
            subprocess.run([str(runtime / 'venv-main/bin/python'), '-B', '-c', command,
                            str(runtime), expected, str(target)], check=True, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
            if (legacy and jobs_snapshot(worker_root) != before
                    or any(digest(read(Path(p))) != sha for p, sha in roles.items())):
                raise ValueError('worker store or role documents changed during install')
            if legacy:
                repo.rename(backup)
                if migrated:
                    write(bootstrap, read(target / 'src/autoslice/runtime_code_lease.py'))
            temporary = overlay / 'repo.next'
            temporary.symlink_to(target)
            os.replace(temporary, repo)
        except BaseException:
            if legacy and backup.exists():
                if repo.is_symlink():
                    repo.unlink()
                backup.rename(repo)
                if old_bootstrap is not None:
                    write(bootstrap, old_bootstrap)
            elif not legacy and old_link is not None and (not repo.is_symlink() or os.readlink(repo) != old_link):
                temporary = overlay / 'repo.restore'
                temporary.symlink_to(old_link)
                os.replace(temporary, repo)
            # Unpublished candidate code is preserved as a recovery artifact.
            if target.exists() and not repo.resolve() == target:
                (overlay / 'repo.rejected').symlink_to(target)
            raise
        result = {'status': 'INSTALLED', 'release_sha256': expected,
                  'commit': manifest['commit'], 'versioned': True,
                  'preimage_path': str(backup) if legacy else old_link,
                  'preserved_job_files': before, 'preserved_role_files': roles}
        write(overlay / 'install.json', encoded(result))
        return result


def ssh(node):
    if not re.fullmatch(r'[A-Za-z0-9_.@-]+', node['host']):
        raise ValueError('invalid overflow SSH host')
    args = ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2']
    if node.get('identity_file'):
        args += ['-i', node['identity_file'], '-o', 'IdentitiesOnly=yes']
    if node.get('known_hosts_file'):
        args += ['-o', 'UserKnownHostsFile=' + node['known_hosts_file']]
    return args + [node['host']]


def _sync_native_audio_nodes(runtime, nodes):
    from src.autoslice import native_audio_runtime as audio
    # Read each provider through the actual ordinary adapter, never CPA/ambient
    # fallbacks. No secret enters the code archive, command line or report.
    payload = {p: audio.validated_native_audio_values(p, environ={'AUTOSLICE_BASE': str(runtime)})
               for p in ('mai', 'moss')}
    source = Path(audio.__file__).read_text()
    bootstrap = source + '''
import sys
sys.path.insert(0,str((Path(sys.argv[1])/'repo').resolve()))
try:
    result=install_native_audio_configuration(Path(sys.argv[1]),json.load(sys.stdin))
except Exception as exc:
    print(json.dumps({'status':'NOT_READY','reason_code':getattr(exc,'reason_code','NATIVE_AUDIO_RUNTIME_CONFIG_INVALID')}))
    sys.exit(1)
print(json.dumps(result))
'''
    results = []
    for node in nodes:
        if Path(node['runtime_root']) != runtime:
            raise ValueError('worker/controller runtime paths differ')
        command = shlex.join(['sudo', '-n', str(runtime / 'venv-main/bin/python'), '-B',
                              '-c', bootstrap, str(runtime)])
        response = subprocess.run(ssh(node) + [command], input=encoded(payload), check=False,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        try:
            result = json.loads(response.stdout)
        except (ValueError, UnicodeError):
            result = {}
        if (response.returncode != 0 or result.get('status') != 'READY'
                or result.get('runtime_root') != str(runtime)
                or {r.get('provider') for r in result.get('providers', []) if r.get('ready') is True} != {'mai', 'moss'}):
            # Do not propagate stderr or subprocess input through diagnostics.
            raise ValueError(f"native audio configuration not ready: {node['host']}")
        results.append({'node': node['host'], **result})
    return results


def sync_native_audio(runtime, config_path):
    """Repair protected provider files without switching active pinned code."""
    runtime, config_path = Path(runtime), Path(config_path)
    with lock(runtime / '.overflow-install.lock'):
        verify_deployed(active_repository(runtime))
        config = json.loads(read(config_path))
        if config.get('mode') not in {'auto', 'remote'} or not config.get('nodes'):
            raise ValueError('native audio sync requires configured auto/remote nodes')
        results = _sync_native_audio_nodes(runtime, config['nodes'])
        return {'status': 'NATIVE_AUDIO_READY', 'workers': results, 'provider_calls': 0,
                'code_switched': False, 'production_or_public_acceptance': False}


@contextlib.contextmanager
def registration_window(runtime):
    """Convert our verified code lease only for final metadata publication."""
    repo = active_repository(runtime)
    if repo.parent.parent == runtime / 'versions':
        with lock(runtime / '.overflow-registration.lock'):
            if (runtime / 'current/repo').resolve() != repo:
                raise ValueError('current version changed before overflow registration')
            yield
        return
    raw = os.environ.get('AUTOSLICE_RUNTIME_CODE_LEASE_FD', '')
    if not (runtime / 'repo/src/autoslice/runtime_code_lease.py').exists():
        raise ValueError('controller runtime code lease is not installed')
    if not raw.isdecimal():
        raise ValueError('controller installation requires a verified runtime code lease')
    fd = int(raw)
    actual = os.fstat(fd)
    expected = (runtime / '.oci3-runtime-code.lock').lstat()
    if (not stat.S_ISREG(expected.st_mode) or expected.st_nlink != 1
            or (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino)):
        raise ValueError('controller installation code lease differs')
    mode = os.environ.get('AUTOSLICE_RUNTIME_CODE_LEASE_MODE')
    os.write(2, b'WAITING_FOR_CONTROLLER_CODE_READERS: final overflow registration\n')
    fcntl.flock(fd, fcntl.LOCK_EX)
    os.environ['AUTOSLICE_RUNTIME_CODE_LEASE_MODE'] = 'exclusive'
    try:
        yield
    finally:
        if mode is None:
            os.environ.pop('AUTOSLICE_RUNTIME_CODE_LEASE_MODE', None)
        else:
            os.environ['AUTOSLICE_RUNTIME_CODE_LEASE_MODE'] = mode
        if mode != 'exclusive':
            fcntl.flock(fd, fcntl.LOCK_SH)


def sync(runtime, config_path, adoptions=None):
    from src.autoslice.execution_backend import _has_unconsumed_assignment, SSHBackend
    runtime, config_path = Path(runtime), Path(config_path)
    adoptions = dict(adoptions or {})
    explicit_adoptions = set(adoptions)
    with lock(runtime / '.overflow-install.lock'):
        original = read(config_path)
        config = json.loads(original)
        if config.get('mode') not in {'auto', 'remote'} or not config.get('nodes'):
            raise ValueError('overflow registration requires configured auto/remote nodes')
        if _has_unconsumed_assignment(runtime):
            raise ValueError('unconsumed overflow assignment; recover before sync')
        repo = active_repository(runtime)
        archive, expected, metadata = snapshot_archive(repo)
        nodes = config['nodes']
        if set(adoptions) - {n['host'] for n in nodes}:
            raise ValueError('adoption supplied for unregistered node')
        for node in nodes:
            declaration = node.get('legacy_adoption_inventory')
            if declaration is not None:
                if (not isinstance(declaration, dict) or set(declaration) != {'path', 'sha256'}
                        or not Path(declaration['path']).is_absolute()
                        or digest(read(Path(declaration['path']))) != declaration['sha256']):
                    raise ValueError('explicit legacy adoption declaration differs')
                adoptions.setdefault(node['host'], declaration['path'])
        results = []
        audio_readiness = _sync_native_audio_nodes(runtime, nodes)
        source = Path(__file__).read_text()
        for node in nodes:
            if Path(node['runtime_root']) != runtime:
                raise ValueError('worker/controller runtime paths differ')
            bootstrap = source + '\nimport sys\nprint(json.dumps(install_worker(sys.stdin.buffer,sys.argv[1],sys.argv[2],sys.argv[3])))\n'
            command = shlex.join(['sudo', '-n', 'python3', '-c', bootstrap,
                                  str(runtime), node['worker_root'], expected])
            installed = subprocess.run(ssh(node) + [command], input=archive, check=True,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
            installation = json.loads(installed.stdout)
            worker = [str(runtime / 'venv-main/bin/python'), '-B',
                      str(runtime / 'versions' / expected / 'repo/scripts/autoslice_overflow_worker.py'),
                      '--worker-root', node['worker_root'], '--allowed-runtime-parent',
                      str(runtime.parent)]
            adoption = None
            probe_command = ssh(node) + [shlex.join(['sudo', '-n', *worker,
                                                  '--runtime-root', str(runtime), 'probe'])]
            response = subprocess.run(probe_command, check=False, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, timeout=120)
            probe = json.loads(response.stdout)
            needs_adoption = (response.returncode != 0 and probe.get('error') == 'worker_authority_unknown')
            if node['host'] in explicit_adoptions or (needs_adoption and node['host'] in adoptions):
                response = subprocess.run(ssh(node) + [shlex.join(['sudo', '-n', *worker, 'adopt-legacy-store'])],
                                          input=read(Path(adoptions[node['host']])), check=True,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
                adoption = json.loads(response.stdout)
                response = subprocess.run(probe_command, check=True, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, timeout=120)
                probe = json.loads(response.stdout)
            if (response.returncode != 0 or probe.get('startup_ok') is not True or probe.get('release_sha256') != expected
                    or probe.get('runtime_root') != str(runtime)
                    or probe.get('worker_root') != node['worker_root']
                    or not re.fullmatch('[0-9a-f]{32}', str(probe.get('worker_store_id', '')))):
                raise ValueError(f"worker registration failed: {node['host']}: {probe.get('startup_error_code')}")
            results.append({'node': node['host'], 'installation': installation,
                            'adoption': adoption, 'probe': probe})
        with registration_window(runtime):
            if read(config_path) != original or _has_unconsumed_assignment(runtime):
                raise ValueError('controller registration changed; preserve config')
            if snapshot_archive(repo)[1] != expected:
                raise ValueError('controller changed before overflow registration')
            config['release_sha256'] = expected
            for node in nodes:
                node['versioned'] = True
            for name, data in metadata.items():
                write(repo / name, data)
            backend = SSHBackend(runtime, config, log=lambda _: None)
            for node in nodes:
                backend._probe_authority(node, backend._rpc(node, 'probe'))
            receipt_dir = runtime / 'feature-overlays' / ('overflow-registration-' + expected)
            receipt_dir.mkdir(parents=True, exist_ok=True)
            preimage = receipt_dir / ('execution-backends.before-' + uuid.uuid4().hex + '.json')
            write(preimage, original)
            write(config_path, encoded(config))
            result = {'status': 'REGISTERED', 'checked_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                      'release_sha256': expected, 'workers': results, 'provider_calls': 0,
                      'native_audio': audio_readiness,
                      'config_preimage_path': str(preimage),
                      'production_or_public_acceptance': False}
            write(receipt_dir / 'registration.json', encoded(result))
            return result
