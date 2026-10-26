#!/usr/bin/env python3
"""Bound one normal OFF successor and preserve an external real process terminal."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def create_json(path: Path, value: dict) -> None:
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    with path.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def bounded_process(command: list[str], *, cwd: Path, prefix: Path, output: Path,
                    head: str, maximum_wall_seconds: int) -> dict:
    if type(maximum_wall_seconds) is not int or not 1 <= maximum_wall_seconds <= 14400:
        raise ValueError("bounded regression wall limit must be 1..14400")
    start_path = prefix.with_suffix(".start.json")
    terminal_path = prefix.with_suffix(".terminal.json")
    log_path = prefix.with_suffix(".worker.log")
    if any(p.exists() or p.is_symlink() for p in (start_path, terminal_path, log_path)):
        raise FileExistsError("bounded regression attempt already exists")
    started = dt.datetime.now(dt.timezone.utc)
    monotonic = time.monotonic()
    with log_path.open("xb") as log:
        os.fchmod(log.fileno(), 0o600)
        process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        start = {"status": "RUNNING", "started_utc": started.isoformat(),
                 "launcher_pid": os.getpid(), "worker_pid": process.pid,
                 "command": command, "cwd": str(cwd), "head": head, "output": str(output),
                 "maximum_wall_seconds": maximum_wall_seconds, "worker_log": str(log_path),
                 "launcher_path": str(Path(__file__).resolve()),
                 "launcher_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                 "argon_enabled": False, "automatic_restart": False}
        create_json(start_path, start)
        timed_out = False
        try:
            process.wait(timeout=max(0.001, maximum_wall_seconds - (time.monotonic() - monotonic)))
        except subprocess.TimeoutExpired:
            timed_out = True
            # The ordinary worker's own alarm fires ten seconds earlier, so
            # it can save its interrupted package. This is the absolute cap.
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        except BaseException:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise
        finally:
            ended = dt.datetime.now(dt.timezone.utc)
            inner = output / "attempt-terminal.json"
            inner_bytes = inner.read_bytes() if inner.is_file() else None
            inner_status = json.loads(inner_bytes).get("status") if inner_bytes else None
            terminal = {**start, "status": inner_status or ("COMPLETED" if process.returncode == 0 else "FAILED"),
                        "returncode": process.returncode, "ended_utc": ended.isoformat(),
                        "elapsed_seconds": time.monotonic() - monotonic,
                        "wall_limit_reached": timed_out,
                        "worker_terminal_path": str(inner),
                        "worker_terminal_sha256": hashlib.sha256(inner_bytes).hexdigest() if inner_bytes else None,
                        "worker_terminal_missing": inner_bytes is None}
            create_json(terminal_path, terminal)
    return terminal


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", type=Path, required=True)
    parser.add_argument("--freeze", type=Path, required=True)
    parser.add_argument("--freeze-sha256", required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--case", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--code-identity", required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--maximum-wall-seconds", type=int, default=14400)
    parser.add_argument("--job-prefix", type=Path, required=True)
    args = parser.parse_args(argv)
    code = Path(__file__).resolve().parents[1]
    command = [sys.executable, "-B", str(code / "scripts/subtitle_regression_eval.py"),
               "--worker", "--normal-off", "--resume-interrupted", str(args.parent),
               "--interruption-evidence", str(args.freeze),
               "--interruption-evidence-sha256", args.freeze_sha256,
               "--cases", str(args.cases), "--output", str(args.output),
               "--code-root", str(code), "--code-identity", args.code_identity,
               "--runtime-root", str(args.runtime_root),
               "--maximum-wall-seconds", str(max(1, args.maximum_wall_seconds - 10))]
    for case in args.case:
        command.extend(["--case", case])
    result = bounded_process(command, cwd=code, prefix=args.job_prefix, output=args.output,
                             head=args.code_identity, maximum_wall_seconds=args.maximum_wall_seconds)
    print(json.dumps({k: result[k] for k in ("status", "returncode", "worker_pid", "output",
                                           "worker_terminal_path")}), flush=True)
    return 0 if result["returncode"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
