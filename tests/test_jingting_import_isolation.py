"""No recording-root I/O for imports/local calls; discovery still selects its root."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Fresh interpreters are necessary: an already imported module hides this fault.
# Guard the filesystem BEFORE import and never actually probe a recording mount.
GUARD = r"""
import ast, builtins, json, os, sys
from pathlib import Path
source = Path('scripts/gemini_slice_jingting.py').read_text()
roots = []
for node in ast.parse(source).body:
    if isinstance(node, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id in {'HOST_VIDEOS', 'CONTAINER_VIDEOS'}
        for t in node.targets
    ):
        roots.append(ast.literal_eval(node.value))
assert len(roots) == 2
probes = []
def protected(fn):
    def checked(path, *args, **kwargs):
        if isinstance(path, (str, bytes, os.PathLike)):
            raw = os.fsdecode(path)
            if any(raw == root or raw.startswith(root + os.sep) for root in roots):
                probes.append(raw)
                raise AssertionError('UNEXPECTED_RECORDING_ROOT_PROBE')
        return fn(path, *args, **kwargs)
    return checked
for name in ('stat', 'lstat', 'open', 'listdir', 'scandir'):
    setattr(os, name, protected(getattr(os, name)))
builtins.open = protected(builtins.open)
def no_external(event, args):
    if event in ('socket.connect', 'subprocess.Popen'):
        raise AssertionError('UNEXPECTED_EXTERNAL_DISPATCH')
sys.addaudithook(no_external)
"""


def fresh(body: str, *, video_root: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("AUTOSLICE_PROFILE", None)
    env.pop("BILIVE_VIDEOS_ROOT", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if video_root is not None:
        env["BILIVE_VIDEOS_ROOT"] = video_root
    return subprocess.run(
        [sys.executable, "-B", "-c", GUARD + "\n" + body],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize(
    "module",
    [
        "scripts.gemini_slice_jingting",
        "src.autoslice.final_subtitle_audio_gate",
        "scripts.produce_slice_package",
    ],
)
def test_fresh_import_never_probes_recording_roots(module):
    result = fresh(f"import importlib; importlib.import_module({module!r}); assert probes == []")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["--help"], 0),
        (["--not-a-valid-option"], 2),
        (["--write-validated-timely-terms", "unused.json"], 2),
        (["--validate-timely-terms", "absent-local-fixture.json"], 2),
    ],
)
def test_offline_cli_never_probes_recording_roots(argv, expected):
    result = fresh(f"""
from scripts import gemini_slice_jingting as module
try:
    code = module.main({argv!r})
except SystemExit as exc:
    code = exc.code
assert code == {expected}, code
assert probes == []
""")
    assert result.returncode == 0, result.stdout + result.stderr


def test_explicit_local_slice_does_not_resolve_a_recording_root():
    result = fresh("""
from scripts import gemini_slice_jingting as module
calls = []
def process(path, provider, **kwargs):
    calls.append((path, provider))
    return 'synthetic process, no actual transcription'
module.process_slice = process
assert module.main(['local-fixture.mp4', '--provider', 'agy']) == 0
assert calls == [('local-fixture.mp4', 'agy')]
assert probes == []
""")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("env_root", [None, "env-root"])
def test_explicit_cli_root_wins_without_implicit_probe(env_root):
    result = fresh(
        """
from scripts import gemini_slice_jingting as module
calls = []
def pending(root, *args, **kwargs):
    calls.append(root)
    return []
module.pending_slices = pending
assert module.main(['--once', '--root', 'explicit-root', '--provider', 'agy']) == 0
assert calls == ['explicit-root']
assert probes == []
""",
        video_root=env_root,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_explicit_environment_keeps_original_import_time_selection():
    result = fresh(
        """
from scripts import gemini_slice_jingting as module
os.environ['BILIVE_VIDEOS_ROOT'] = 'later-change'
calls = []
def pending(root, *args, **kwargs):
    calls.append(root)
    return []
module.pending_slices = pending
assert module.main(['--once', '--provider', 'agy']) == 0
assert calls == ['frozen-env-root']
assert probes == []
""",
        video_root="frozen-env-root",
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("host_present", [True, False])
def test_discovery_preserves_host_container_fallback(tmp_path, monkeypatch, host_present):
    from scripts import gemini_slice_jingting as module

    host, container = tmp_path / "host", tmp_path / "container"
    if host_present:
        host.mkdir()
    container.mkdir()
    monkeypatch.setattr(module, "VIDEOS", None)
    monkeypatch.setattr(module, "HOST_VIDEOS", str(host))
    monkeypatch.setattr(module, "CONTAINER_VIDEOS", str(container))
    seen = []
    real_isdir = module.os.path.isdir

    def isdir(path):
        if os.fspath(path) == str(host):
            seen.append(path)
        return real_isdir(path)

    def pending(root, *_args, **_kwargs):
        assert root == str(host if host_present else container)
        return []

    monkeypatch.setattr(module.os.path, "isdir", isdir)
    monkeypatch.setattr(module, "pending_slices", pending)
    assert module.main(["--once", "--provider", "agy"]) == 0
    assert seen == [str(host)]


def test_default_discovery_does_not_suppress_root_probe_failure(monkeypatch):
    from scripts import gemini_slice_jingting as module

    monkeypatch.setattr(module, "VIDEOS", None)

    def fault(_path):
        raise RuntimeError("synthetic source probe failure")

    def forbidden(*_args, **_kwargs):
        pytest.fail("failed discovery root reached candidate enumeration")

    monkeypatch.setattr(module.os.path, "isdir", fault)
    monkeypatch.setattr(module, "pending_slices", forbidden)
    with pytest.raises(RuntimeError, match="synthetic source probe failure"):
        module.main(["--once", "--provider", "agy"])


def test_explicit_host_environment_is_not_stat_ed_during_import():
    # The configured path may itself be on FUSE; reading its string is enough.
    import ast

    tree = ast.parse((ROOT / "scripts/gemini_slice_jingting.py").read_text())
    host = next(
        ast.literal_eval(n.value)
        for n in tree.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "HOST_VIDEOS" for t in n.targets)
    )
    result = fresh(
        """
from src.autoslice import final_subtitle_audio_gate
assert probes == []
""",
        video_root=host,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("host_present", [True, False])
def test_real_directory_discovery_still_finds_local_fixture(tmp_path, monkeypatch, host_present):
    from scripts import gemini_slice_jingting as module

    host, container = tmp_path / "host", tmp_path / "container"
    selected = host if host_present else container
    date_root = selected / "123456" / "2026-01-01"
    date_root.mkdir(parents=True)
    stem = "1s_example_123456_2026-01-01-00-00-00-"
    video = date_root / (stem + ".mp4")
    video.write_bytes(b"discovery fixture, not actual media")
    subtitles = date_root / "subtitles"
    subtitles.mkdir()
    (subtitles / (stem + ".srt")).write_text("1\n00:00:00,000 --> 00:00:01,000\nexample\n")
    monkeypatch.setattr(module, "VIDEOS", None)
    monkeypatch.setattr(module, "HOST_VIDEOS", str(host))
    monkeypatch.setattr(module, "CONTAINER_VIDEOS", str(container))
    calls = []

    def process(path, provider, **kwargs):
        calls.append((path, provider))
        return "synthetic processing only"

    monkeypatch.setattr(module, "process_slice", process)
    assert module.main(["--once", "--room", "123456", "--provider", "agy"]) == 0
    assert calls == [(str(video), "agy")]


def test_daemon_keeps_lock_lifecycle_and_selected_root(tmp_path, monkeypatch):
    from scripts import gemini_slice_jingting as module

    run_dir = tmp_path / "run"
    real_path = module.Path
    monkeypatch.setattr(
        module, "Path", lambda path: run_dir if path == "/opt/bilive/run" else real_path(path)
    )
    monkeypatch.setattr(module, "VIDEOS", "configured-root")
    operations = []
    monkeypatch.setattr(
        module,
        "acquire_directory_lock",
        lambda path: operations.append(("acquire", str(path))) or True,
    )
    monkeypatch.setattr(
        module, "release_directory_lock", lambda path: operations.append(("release", str(path)))
    )

    def pending(root, *_args, **_kwargs):
        operations.append(("scan", root))
        return []

    def stop(_seconds):
        raise RuntimeError("stop synthetic daemon after one sweep")

    monkeypatch.setattr(module, "pending_slices", pending)
    monkeypatch.setattr(module.time, "sleep", stop)
    with pytest.raises(RuntimeError, match="stop synthetic daemon"):
        module.main(["--daemon", "--room", "123456", "--provider", "agy"])
    path = str(run_dir / "jingting-123456.daemon.lock")
    assert operations == [("acquire", path), ("scan", "configured-root"), ("release", path)]
