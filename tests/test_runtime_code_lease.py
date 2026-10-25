"""Real process locks: concurrent readers, writer wait, crash and recovery."""

import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("width", [40, 64])
def test_versioned_producer_uses_shared_runtime_credentials_and_cache(tmp_path, monkeypatch, width):
    from src.autoslice.runtime_code_lease import repository_runtime_root
    import scripts.produce_slice_package as producer

    repo = tmp_path / "versions" / ("a" * width) / "repo"
    repo.mkdir(parents=True)
    (repo / "DEPLOYED_COMMIT").write_text("a" * 40)
    (repo / "DEPLOYED_MANIFEST.json").write_text("{}")
    if width == 64:
        (repo / "OVERFLOW_RELEASE_SHA256").write_text("a" * 64)
    (tmp_path / "cpa.env").touch()
    assert repository_runtime_root(repo) == tmp_path
    assert (repository_runtime_root(repo) / "cpa.env").is_file()
    monkeypatch.setattr(producer, "ROOT", repo)
    monkeypatch.delenv("AUTOSLICE_BASE", raising=False)
    monkeypatch.delenv("AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT", raising=False)
    assert producer.profile_external_audio_cache_root() == tmp_path / "cache/nested-external-audio"
    assert repository_runtime_root(tmp_path / "development") == tmp_path


def setup_runtime(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "DEPLOYED_COMMIT").write_text("a" * 40)
    (tmp_path / ".oci3-runtime-code.lock").touch()
    wrapper = tmp_path / "runtime-code-lease.py"
    shutil.copyfile(ROOT / "src/autoslice/runtime_code_lease.py", wrapper)
    return repo, wrapper


def wait_file(path, process):
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        assert process.poll() is None, process.communicate()
        time.sleep(0.01)
    assert path.exists()


def test_two_readers_share_lease_and_writer_resumes_after_exit(tmp_path):
    repo, wrapper = setup_runtime(tmp_path)
    script = repo / "scripts/read.py"
    script.write_text(
        "import pathlib,sys,time\npathlib.Path(sys.argv[1]).touch()\nwhile True: time.sleep(.1)\n"
    )
    readers = [
        subprocess.Popen(
            [sys.executable, str(wrapper), str(repo), str(script), str(tmp_path / f"reader{i}")]
        )
        for i in range(2)
    ]
    writer = None
    try:
        for i, p in enumerate(readers):
            wait_file(tmp_path / f"reader{i}", p)
        writer = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import fcntl,sys,pathlib; f=open(sys.argv[1]); fcntl.flock(f,fcntl.LOCK_EX); pathlib.Path(sys.argv[2]).touch()",
                str(tmp_path / ".oci3-runtime-code.lock"),
                str(tmp_path / "writer"),
            ]
        )
        time.sleep(0.1)
        assert not (tmp_path / "writer").exists()
        readers[0].terminate()
        readers[0].wait(timeout=3)
        time.sleep(0.1)
        assert not (tmp_path / "writer").exists()
        readers[1].kill()
        readers[1].wait(timeout=3)
        writer.wait(timeout=3)
        assert writer.returncode == 0 and (tmp_path / "writer").exists()
    finally:
        for p in [*readers, writer]:
            if p and p.poll() is None:
                p.kill()
                p.wait()


def test_queued_reader_loads_fresh_bytes_and_incomplete_transaction_refuses(tmp_path):
    repo, wrapper = setup_runtime(tmp_path)
    script = repo / "scripts/read.py"
    script.write_text('print("old")')
    with (tmp_path / ".oci3-runtime-code.lock").open() as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        child = subprocess.Popen(
            [sys.executable, str(wrapper), str(repo), str(script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            time.sleep(0.1)
            assert child.poll() is None
            script.write_text('print("new")')
            fcntl.flock(lock, fcntl.LOCK_UN)
            out, err = child.communicate(timeout=3)
            assert child.returncode == 0 and out.strip() == "new", err
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
    residue = tmp_path / ".oci3-shadow-autoslice-interrupted"
    residue.mkdir()
    blocked = subprocess.run(
        [sys.executable, str(wrapper), str(repo), str(script)], capture_output=True, text=True
    )
    assert blocked.returncode == 75 and "RECOVERY_REQUIRED" in blocked.stderr and not blocked.stdout


def test_private_library_import_holds_lease(tmp_path):
    repo, wrapper = setup_runtime(tmp_path)
    package = repo / "src/autoslice"
    package.mkdir(parents=True)
    shutil.copyfile(ROOT / "src/autoslice/__init__.py", package / "__init__.py")
    code = "import sys,pathlib,time; sys.path.insert(0,sys.argv[1]); import src.autoslice; pathlib.Path(sys.argv[2]).touch(); time.sleep(30)"
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(repo), str(tmp_path / "imported")], cwd=tmp_path
    )
    try:
        wait_file(tmp_path / "imported", child)
        with (tmp_path / ".oci3-runtime-code.lock").open() as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise AssertionError("library reader did not hold lease")
        child.terminate()
        child.wait(timeout=3)
        with (tmp_path / ".oci3-runtime-code.lock").open() as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_version_switch_preserves_inflight_code_and_new_entry(tmp_path):
    versions = tmp_path / "versions"
    wrapper = ROOT / "src/autoslice/runtime_code_lease.py"
    for letter in ("a", "b"):
        repo = versions / (letter * 40) / "repo"
        (repo / "scripts").mkdir(parents=True)
        (repo / "DEPLOYED_COMMIT").write_text(letter * 40)
        (repo / "DEPLOYED_MANIFEST.json").write_text("{}")
        (repo / "asset").write_text(letter)
        (repo / "scripts/read.py").write_text(
            "from pathlib import Path\nimport sys,time\n"
            "root=Path(__file__).resolve().parents[1]\n"
            "if len(sys.argv)>1: Path(sys.argv[1]).touch(); time.sleep(.5)\n"
            'print((root/"asset").read_text())\n'
        )
    current = tmp_path / "current"
    current.symlink_to(versions / ("a" * 40), target_is_directory=True)
    logical = current / "repo"
    started = tmp_path / "started"
    old = subprocess.Popen(
        [
            sys.executable,
            str(wrapper),
            str(logical),
            str(logical / "scripts/read.py"),
            str(started),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_file(started, old)
        replacement = tmp_path / "replacement"
        replacement.symlink_to(versions / ("b" * 40), target_is_directory=True)
        replacement.replace(current)
        new = subprocess.run(
            [sys.executable, str(wrapper), str(logical), str(logical / "scripts/read.py")],
            capture_output=True,
            text=True,
        )
        out, err = old.communicate(timeout=3)
        assert old.returncode == 0 and out.strip() == "a", err
        assert new.returncode == 0 and new.stdout.strip() == "b", new.stderr
        assert not (tmp_path / ".oci3-runtime-code.lock").exists()
    finally:
        if old.poll() is None:
            old.kill()
            old.wait()


def test_child_inherits_old_version_after_current_switch(tmp_path):
    wrapper = ROOT / "src/autoslice/runtime_code_lease.py"
    for letter in ("a", "b"):
        repo = tmp_path / "versions" / (letter * 40) / "repo"
        (repo / "scripts").mkdir(parents=True)
        (repo / "DEPLOYED_COMMIT").write_text(letter * 40)
        (repo / "DEPLOYED_MANIFEST.json").write_text("{}")
        (repo / "scripts/read.py").write_text(f'print("{letter}")')
    current = tmp_path / "current"
    current.symlink_to(tmp_path / "versions" / ("b" * 40), target_is_directory=True)
    logical = current / "repo"
    env = os.environ.copy()
    env["AUTOSLICE_RUNTIME_VERSION_ROOT"] = str(tmp_path / "versions" / ("a" * 40) / "repo")
    proc = subprocess.run(
        [sys.executable, str(wrapper), str(logical), str(logical / "scripts/read.py")],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0 and proc.stdout.strip() == "a", proc.stderr
