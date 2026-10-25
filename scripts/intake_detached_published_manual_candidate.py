#!/usr/bin/env python3
"""Plan or atomically project an already-public native manual Talk package.

The default mode creates only a private, create-only plan receipt.  It never
uploads, polls publication, creates ``review_ready``, or changes daily state.
``--apply`` is deliberately guarded by the existing maintenance pause and both
native state locks; it still projects factual publication evidence only.
"""

from __future__ import annotations

import argparse
import datetime as _datetime
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.detached_manual_publication_intake import (
    plan_detached_publication_intake,
    verified_detached_manual_inputs,
)
from src.autoslice.package_import import atomic_write_bytes, exclusive_lock
from src.autoslice.publication_state_projection import _reconciliation_lock
from src.autoslice.publication_reconciliation import _candidate_and_date
from src.autoslice.publication_state_projection import PublicationReconciliationError


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")


def _create_exclusive(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _load_plan(state_path: Path, manifest_path: Path, bvid: str) -> tuple[bytes, dict, dict, dict]:
    if state_path.is_symlink() or not state_path.is_file():
        raise PublicationReconciliationError("native daily state is not a regular file")
    before = state_path.read_bytes()
    try:
        state = json.loads(before)
    except (TypeError, ValueError) as exc:
        raise PublicationReconciliationError("native daily state is not valid JSON") from exc
    verified = verified_detached_manual_inputs(manifest_path, bvid)
    after, plan = plan_detached_publication_intake(
        state,
        verified,
        state_sha256=_sha256(before),
    )
    return before, after, plan, verified


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--bvid", required=True)
    parser.add_argument("--base", type=Path, default=Path("/opt/bilive/autoslice"))
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    if args.receipt.exists() or args.receipt.is_symlink():
        raise ValueError("detached intake receipt already exists")

    from scripts import authorized_upload as uploader

    manifest, problems = uploader.load_and_verify(args.manifest, ordinary_upload=True)
    if problems:
        raise ValueError("native manual manifest validation failed")
    _candidate, date = _candidate_and_date(manifest)
    state_path = args.base / "state" / (date + ".json")
    if args.apply and not (args.base / "DISABLED").is_file():
        raise ValueError("detached publication intake --apply requires maintenance pause")

    if not args.apply:
        before, after, plan, _verified = _load_plan(state_path, args.manifest, args.bvid)
        payload = _json_bytes(after)
        result = {
            "schema_version": "detached-published-manual-intake-result.v1",
            "at": _datetime.datetime.now(_datetime.timezone.utc).isoformat(),
            "run_mode": "DRY_RUN",
            "plan": plan,
            "intake_receipt": plan.get("receipt"),
            "before_sha256": _sha256(before),
            "planned_after_sha256": _sha256(payload),
            "state_path": str(state_path),
            "state_written": False,
            "new_uploads": 0,
            "provider_calls": 0,
        }
        _create_exclusive(args.receipt, _json_bytes(result))
        print(json.dumps(result, ensure_ascii=False))
        return 0

    with exclusive_lock(args.base / "runner.lock", label="detached published manual intake"):
        with _reconciliation_lock(args.base):
            if not (args.base / "DISABLED").is_file():
                raise ValueError("maintenance pause changed before detached intake")
            before, after, plan, _verified = _load_plan(state_path, args.manifest, args.bvid)
            payload = _json_bytes(after)
            result = {
                "schema_version": "detached-published-manual-intake-result.v1",
                "at": _datetime.datetime.now(_datetime.timezone.utc).isoformat(),
                "run_mode": "APPLY",
                "plan": plan,
                "intake_receipt": plan.get("receipt"),
                "before_sha256": _sha256(before),
                "planned_after_sha256": _sha256(payload),
                "state_path": str(state_path),
                "state_written": False,
                "new_uploads": 0,
                "provider_calls": 0,
            }
            if plan.get("state_changed"):
                backup = args.receipt.with_suffix(".state-before.json")
                prepared = args.receipt.with_suffix(".prepared.json")
                _create_exclusive(backup, before)
                _create_exclusive(prepared, _json_bytes(result))
                if state_path.read_bytes() != before:
                    raise RuntimeError("daily state changed before detached intake CAS")
                atomic_write_bytes(state_path, payload)
                if state_path.read_bytes() != payload:
                    raise RuntimeError("daily state readback differs after detached intake")
                result.update(
                    state_written=True, backup_path=str(backup), prepared_path=str(prepared)
                )
            _create_exclusive(args.receipt, _json_bytes(result))
            print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PublicationReconciliationError, ValueError, OSError) as exc:
        raise SystemExit(str(exc))
