"""The shared quiet gate distinguishes execution positions and fails closed."""
from __future__ import annotations

import importlib
import io
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
planner = importlib.import_module("cleanup_preflight_scan")


@pytest.mark.parametrize("name,argv,busy", [
    ("EmbyServer", None, False),
    ("ffmpeg", None, True),
    ("ffmpeg-7.1", None, True),
    ("grep", None, False),
    ("python3", ["python3", "-c", "import time; time.sleep(2)", "--ffmpeg", "/bin/ffmpeg"], False),
    ("python3", ["python3", "observer.py", "--watch", "/any/produce_slice_package.py"], False),
    ("python3", ["python3", "-u", "/any/session_autoslice.py", "grep"], True),
    ("python3", ["python3", "/any/produce_slice_package.py", "cleanup_preflight"], True),
    ("python3", ["python3", "-W", "ignore", "-X", "dev", "--", "/any/produce_slice_package.py"], True),
    ("python3", ["python3", "-m", "scripts.session_autoslice", "cleanup_preflight"], True),
    ("python3", ["python3", "-um", "scripts.produce_slice_package"], True),
    ("python3", ["python3", "-c", "import subprocess; subprocess.run(['ffmpeg'])"], True),
    ("python3", ["python3", "cleanup_preflight_scan.py", "--base", "/ffmpeg/data"], False),
    ("bash", ["bash", "-lc", "python3 scripts/session_autoslice.py --label grep"], True),
    ("bash", ["bash", "-c", "printf hello", "ffmpeg"], False),
    ("bash", ["-bash"], False),
    ("bash", ["bash", "-i"], False),
    ("bash", ["bash", "--rcfile", "/config/ffmpeg.rc", "-lc", "printf hello"], False),
    ("bash", ["bash", "--noprofile", "--norc", "-lc", "ffmpeg"], True),
    ("bash", ["bash", "--rcfile", "/config/normal.rc", "-lc", "ffmpeg"], True),
    ("flock", ["flock", "-n", "/any/runner.lock", "bash", "-lc", "python3 produce_slice_package.py"], True),
    ("flock", ["flock", "-n", "9"], False),
    ("flock", ["flock", "/ffmpeg/runner.lock", "python3", "observer.py"], False),
    ("flock", ["flock", "/lock", "-c", "ffmpeg -metadata comment=grep"], True),
    ("env", ["env", "FFMPEG=/bin/ffmpeg", "EmbyServer", "-ffmpeg", "/bin/ffmpeg"], False),
    ("env", ["env", "-u", "FFMPEG", "python3", "session_autoslice.py"], True),
    ("timeout", ["timeout", "--signal=TERM", "5", "ffmpeg", "cleanup_preflight"], True),
])
def test_execution_position(name, argv, busy):
    assert bool(planner._production_role(name, argv)) is busy


@pytest.mark.parametrize("name,argv", [
    ("", None), ("python3", None), ("python3", ["python3", "-"]),
    ("python3", ["python3", "-m"]), ("python3", ["python3", "--unknown", "observer.py"]),
    ("bash", ["bash", "-c"]), ("env", ["env", "-S", "ffmpeg"]),
    ("bash", ["bash", "-"]),
    ("bash", ["bash", "-s"]), ("bash", ["bash", "-ls"]),
    ("bash", ["bash", "--rcfile"]), ("bash", ["bash", "--unknown", "-c", "ffmpeg"]),
    ("bash", ["bash", "+unknown", "-c", "ffmpeg"]),
    ("timeout", ["timeout", "5"]),
])
def test_unidentified_execution_entry_holds(name, argv):
    with pytest.raises(ValueError):
        planner._production_role(name, argv)


@pytest.fixture
def quiet_case(tmp_path, monkeypatch):
    (tmp_path / "DISABLED").touch()
    monkeypatch.setattr(sys, "platform", "linux")
    return str(tmp_path)


@pytest.mark.parametrize("reply", [
    subprocess.CompletedProcess([], 1, "", "private stderr must not appear"),
    subprocess.CompletedProcess([], 0, "", ""),
    subprocess.CompletedProcess([], 0, "pid command", ""),
    subprocess.CompletedProcess([], 0, "0 ffmpeg", ""),
    subprocess.CompletedProcess([], 0, "12 EmbyServer\n12 ffmpeg", ""),
    OSError("ps unavailable"), subprocess.TimeoutExpired("ps", 5),
])
def test_ps_failure_is_not_quiet(quiet_case, monkeypatch, reply):
    def run(*args, **kwargs):
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(planner.subprocess, "run", run)
    problems = planner.quiet_window(quiet_case)
    assert problems and "production process scan" in problems[0]
    assert "private stderr" not in str(problems)


def test_public_native_identity_does_not_require_privileged_exe_or_argv(quiet_case, monkeypatch):
    monkeypatch.setattr(planner.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess([], 0, "12 EmbyServer\n13 ffmpeg", ""))
    monkeypatch.setattr(planner, "_process_argv", lambda *a:
                        pytest.fail("native comm should suffice, without privileged exe/argv"))
    assert planner.quiet_window(quiet_case) == ["in-flight production process: PID 13 (ffmpeg)"]


@pytest.mark.parametrize("error", [PermissionError(), ValueError(), UnicodeError(), FileNotFoundError()])
def test_required_interpreter_read_failure_holds(quiet_case, monkeypatch, error):
    monkeypatch.setattr(planner.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess([], 0, "12 python3", ""))
    def unreadable(*args):
        raise error
    monkeypatch.setattr(planner, "_process_argv", unreadable)
    monkeypatch.setattr(Path, "stat", lambda *a, **k: object())
    assert planner.quiet_window(quiet_case) == ["production process identity unreadable: PID 12"]


def test_process_that_exited_is_distinct_from_a_read_error(quiet_case, monkeypatch):
    monkeypatch.setattr(planner.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess([], 0, "987654321 python3", ""))
    def exited(*args):
        raise FileNotFoundError()
    monkeypatch.setattr(planner, "_process_argv", exited)
    assert planner.quiet_window(quiet_case) == []


@pytest.mark.parametrize("comm,argv,busy", [
    ("python3", ["python3", "observer.py", "ffmpeg"], False),
    ("free_session_au", ["python3", "session_autoslice.py", "grep"], True),
    ("produce_slice_p", ["python3", "produce_slice_package.py", "cleanup_preflight"], True),
    ("bash", ["-bash", "-lc", "ffmpeg -metadata comment=grep"], True),
    ("bash", ["bash", "-ls"], True),
    ("python3", ["unknown", "observer.py"], True),
])
def test_public_comm_and_structured_entry_scan(quiet_case, monkeypatch, comm, argv, busy):
    monkeypatch.setattr(planner.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess([], 0, f"12 {comm}", ""))
    monkeypatch.setattr(planner, "_process_argv", lambda *a: argv)
    assert bool(planner.quiet_window(quiet_case)) is busy


def test_nul_argv_preserves_empty_arguments_and_spaces(monkeypatch):
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: "python3\n")
    monkeypatch.setattr(Path, "open", lambda *a, **k:
                        io.BytesIO(b"python3\0-c\0\0--file\0/space path/ffmpeg\0"))
    assert planner._process_argv(12, "python3") == ["python3", "-c", "", "--file", "/space path/ffmpeg"]


@pytest.mark.parametrize("raw,comm", [(b"", "python3\n"), (b"python3", "python3\n"),
                                       (b"x" * 65537, "python3\n"), (b"python3\0", "ffmpeg\n")])
def test_unreadable_or_changed_entry_is_rejected(monkeypatch, raw, comm):
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: comm)
    monkeypatch.setattr(Path, "open", lambda *a, **k: io.BytesIO(raw))
    with pytest.raises(ValueError):
        planner._process_argv(12, "python3")


@pytest.mark.parametrize("holders,ancestors,blocked", [("12 13", {12}, False), ("13", {12}, True)])
def test_original_own_ancestor_flock_rule_remains(quiet_case, monkeypatch, holders, ancestors, blocked):
    Path(quiet_case, "runner.lock").touch()
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, holders if argv[0] == "fuser" else "1 systemd", "")
    monkeypatch.setattr(planner.subprocess, "run", run)
    monkeypatch.setattr(planner, "_own_process_ancestry", lambda: ancestors)
    assert bool(planner.quiet_window(quiet_case)) is blocked
