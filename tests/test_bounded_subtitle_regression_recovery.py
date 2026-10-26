"""The durable wrapper tests actual subprocess termination, never providers."""
import json
import os
import signal
import sys

import pytest

from scripts.run_bounded_subtitle_regression_recovery import bounded_process


@pytest.mark.parametrize("timeout", [False, True])
def test_bounded_process_records_real_exit_and_refuses_repeat(tmp_path, timeout):
    prefix = tmp_path / "job"
    output = tmp_path / "output"
    command = [sys.executable, "-B", "-c",
               "import time; print('synthetic child started', flush=True); time.sleep(5)" if timeout
               else "print('synthetic child returned', flush=True)"]
    result = bounded_process(command, cwd=tmp_path, prefix=prefix, output=output,
                             head="synthetic", maximum_wall_seconds=1 if timeout else 10)
    assert result["wall_limit_reached"] is timeout
    assert result["returncode"] == (-signal.SIGKILL if timeout else 0)
    assert result["worker_terminal_missing"] is True
    assert result["worker_terminal_sha256"] is None
    assert prefix.with_suffix(".worker.log").read_bytes()
    start = json.loads(prefix.with_suffix(".start.json").read_bytes())
    end = prefix.with_suffix(".terminal.json").read_bytes()
    assert start["worker_pid"] == result["worker_pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(start["worker_pid"], 0)
    with pytest.raises(FileExistsError):
        bounded_process(command, cwd=tmp_path, prefix=prefix, output=output,
                        head="synthetic", maximum_wall_seconds=1)
    assert prefix.with_suffix(".terminal.json").read_bytes() == end


def test_worker_terminal_is_bound_as_real_bytes(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    terminal = output / "attempt-terminal.json"
    terminal.write_text('{"status":"INTERRUPTED","returncode":-15}')
    result = bounded_process([sys.executable, "-B", "-c", "raise SystemExit(143)"],
        cwd=tmp_path, prefix=tmp_path / "job", output=output,
        head="synthetic", maximum_wall_seconds=10)
    assert result["status"] == "INTERRUPTED" and result["returncode"] == 143
    assert result["worker_terminal_sha256"] and not result["worker_terminal_missing"]
