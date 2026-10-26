#!/usr/bin/env python3
"""CLI for the bounded autoslice overflow worker protocol.

The controller submits a JSON request on stdin.  All other commands emit one
JSON object on stdout and keep diagnostics out of the response; result files
are described by path and hash for a caller-owned transfer step.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice import overflow_worker


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--worker-root",
        type=Path,
        default=overflow_worker.DEFAULT_WORKER_ROOT,
        help="durable control root (default: /data/autoslice-worker/control)",
    )
    parser.add_argument(
        "--allowed-runtime-parent",
        type=Path,
        default=None,
        help="explicit absolute parent which must contain runtime_root",
    )
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=None,
        help="configured logical runtime root (must match request when provided)",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    probe = subparsers.add_parser("probe", help="inspect safe worker capability metadata")
    probe.add_argument("--runtime-root", dest="probe_runtime_root", type=Path, default=None)
    probe.add_argument("--release-marker", type=Path, default=None)

    subparsers.add_parser("submit", help="admit a request read from stdin")
    subparsers.add_parser("adopt-legacy-store", help="explicitly verify a legacy inventory/receipts from stdin and establish future store authority")

    for name in ("status", "fetch-description"):
        command = subparsers.add_parser(name)
        command.add_argument("--job-id", required=True)
        command.add_argument("--attempt-id", required=True)

    run = subparsers.add_parser("run", help=argparse.SUPPRESS)
    run.add_argument("--job-id", required=True)
    run.add_argument("--attempt-id", required=True)
    run.add_argument("--slot-fd", type=int, default=None)
    run.add_argument("--start-fd", type=int, default=None)
    return parser


def _safe_error(exc: BaseException) -> dict[str, Any]:
    code = getattr(exc, "code", None)
    if not isinstance(code, str) or not code or any(ch not in "abcdefghijklmnopqrstuvwxyz_" for ch in code):
        code = "internal_error"
    return {"ok": False, "error": code}


def _read_request() -> dict[str, Any]:
    try:
        value = json.load(sys.stdin)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise overflow_worker.RequestValidationError("request stdin is invalid") from exc
    if not isinstance(value, dict):
        raise overflow_worker.RequestValidationError("request stdin is not an object")
    return value


def _emit(value: dict[str, Any]) -> None:
    # The response itself is deliberately restricted to metadata assembled by
    # overflow_worker.  A final newline makes SSH JSON capture deterministic.
    sys.stdout.write(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _apply_optional_limits() -> None:
    """Lower priority by default; hard limits are opt-in deployment knobs."""
    try:
        os.nice(int(os.environ.get("AUTOSLICE_OVERFLOW_NICE", "10")))
    except (OSError, ValueError):
        pass
    for env_name, limit_name in (("AUTOSLICE_OVERFLOW_CPU_SECONDS", "RLIMIT_CPU"),
                                 ("AUTOSLICE_OVERFLOW_MEMORY_BYTES", "RLIMIT_AS")):
        raw = os.environ.get(env_name)
        if not raw:
            continue
        try:
            value = int(raw)
            if value > 0:
                resource.setrlimit(getattr(resource, limit_name), (value, value))
        except (OSError, ValueError, AttributeError):
            pass


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.action == "probe":
            result = overflow_worker.probe(
                worker_root=args.worker_root,
                runtime_root=args.runtime_root or args.probe_runtime_root,
                release_marker=args.release_marker,
            )
        elif args.action == "adopt-legacy-store":
            if args.allowed_runtime_parent is None:
                raise overflow_worker.PathValidationError("allowed runtime parent is required")
            result = overflow_worker.adopt_legacy_store(
                _read_request(), worker_root=args.worker_root,
                allowed_runtime_parent=args.allowed_runtime_parent,
                expected_runtime_root=args.runtime_root,
            )
        elif args.action == "submit":
            if args.allowed_runtime_parent is None:
                raise overflow_worker.PathValidationError("allowed runtime parent is required")
            result = overflow_worker.submit_request(
                _read_request(),
                worker_root=args.worker_root,
                allowed_runtime_parent=args.allowed_runtime_parent,
                expected_runtime_root=args.runtime_root,
            )
        elif args.action == "status":
            if args.allowed_runtime_parent is None:
                raise overflow_worker.PathValidationError("allowed runtime parent is required")
            result = overflow_worker.status_job(
                worker_root=args.worker_root,
                job_id=args.job_id,
                attempt_id=args.attempt_id,
                allowed_runtime_parent=args.allowed_runtime_parent,
                expected_runtime_root=args.runtime_root,
            )
        elif args.action == "run":
            if args.allowed_runtime_parent is None:
                raise overflow_worker.PathValidationError("allowed runtime parent is required")
            _apply_optional_limits()
            result = overflow_worker.run_job(
                worker_root=args.worker_root,
                job_id=args.job_id,
                attempt_id=args.attempt_id,
                allowed_runtime_parent=args.allowed_runtime_parent,
                expected_runtime_root=args.runtime_root,
                slot_fd=args.slot_fd,
                start_fd=args.start_fd,
            )
        elif args.action == "fetch-description":
            if args.allowed_runtime_parent is None:
                raise overflow_worker.PathValidationError("allowed runtime parent is required")
            result = overflow_worker.fetch_description(
                worker_root=args.worker_root,
                job_id=args.job_id,
                attempt_id=args.attempt_id,
                allowed_runtime_parent=args.allowed_runtime_parent,
                expected_runtime_root=args.runtime_root,
            )
        else:  # pragma: no cover - argparse enforces the choices
            raise overflow_worker.OverflowWorkerError("unsupported action")
        _emit(result)
        if result.get("status") == "failed" and args.action == "run":
            return 1
        return 0
    except BaseException as exc:
        _emit(_safe_error(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
