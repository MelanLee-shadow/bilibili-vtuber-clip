#!/usr/bin/env python3
"""Diagnose one frozen Jev request/body without changing any acceptance rule.

No SDK, credentials, network, labels, subtitle edits or provider requests.
A successful command means only that the diagnostic was produced.
"""

from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.autoslice.jev_response_diagnostics import diagnose_response  # noqa: E402

MAX_INPUT_BYTES = 16 * 1024 * 1024


def read_frozen(path: Path) -> bytes:
    absolute = path.absolute()
    cursor = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        cursor /= component
        if stat.S_ISLNK(cursor.lstat().st_mode):
            raise ValueError("INPUT_SYMLINK")
    fd = os.open(absolute, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_INPUT_BYTES:
            raise ValueError("INPUT_NOT_BOUNDED_REGULAR_FILE")
        with os.fdopen(os.dup(fd), "rb") as file:
            raw = file.read(MAX_INPUT_BYTES + 1)
        after = os.fstat(fd)
        if len(raw) != before.st_size or (
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("INPUT_CHANGED_DURING_READ")
        return raw
    finally:
        os.close(fd)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def diagnose_files(request: Path, response: Path, *, expected_model: str) -> dict[str, Any]:
    req, rsp = read_frozen(request), read_frozen(response)
    result = diagnose_response(
        json.loads(req, object_pairs_hook=_unique_object),
        json.loads(rsp, object_pairs_hook=_unique_object),
        expected_model=expected_model,
    )
    result["input_sha256"] = {
        "request": hashlib.sha256(req).hexdigest(),
        "response": hashlib.sha256(rsp).hexdigest(),
    }
    result["input_bytes"] = {"request": len(req), "response": len(rsp)}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument(
        "--expected-model",
        required=True,
        help="Exact model from the frozen experiment configuration, not an inferred alias.",
    )
    parser.add_argument(
        "--output", type=Path, help="Create-only diagnostic file. Default: stdout only."
    )
    args = parser.parse_args()
    try:
        result = diagnose_files(args.request, args.response, expected_model=args.expected_model)
        data = (json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
        if args.output is None:
            sys.stdout.write(data.decode())
        else:
            # Parent must already exist; no recursive creation or replacement.
            absolute = args.output.absolute()
            cursor = Path(absolute.anchor)
            for component in absolute.parts[1:-1]:
                cursor /= component
                if stat.S_ISLNK(cursor.lstat().st_mode):
                    raise ValueError("OUTPUT_PARENT_SYMLINK")
            fd = os.open(absolute, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
        return 0
    except (ValueError, OSError, TypeError) as exc:
        print(f"DIAGNOSTIC_NOT_WRITTEN: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
