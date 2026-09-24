#!/usr/bin/env python3
"""Reconcile an already-public native manual package with its exact queued source.

Default dry-run. --apply requires maintenance DISABLED and writes one state CAS;
never uploads, creates a review_ready verdict, opens source media mounts, or
changes provider/processing/approval settings. Run native season-add afterward
for the same existing BVID to complete runtime-registry/ledger reconciliation.
"""
from __future__ import annotations
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.manual_publication_intake import (  # noqa: E402
    already_applied_manual_intake, plan_manual_publication_intake, verified_manual_inputs,
)
from src.autoslice.package_import import atomic_write_bytes, exclusive_lock  # noqa: E402
from src.autoslice.publication_state_projection import _reconciliation_lock  # noqa: E402


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
        raise ValueError("create-only intake receipt already exists")
    if not (args.base / "DISABLED").is_file():
        raise ValueError("published-manual intake requires the existing maintenance pause")
    from scripts import authorized_upload as uploader
    from src.autoslice.publication_reconciliation import _candidate_and_date
    manifest, problems = uploader.load_and_verify(args.manifest, ordinary_upload=True)
    if problems:
        raise ValueError("native manual manifest validation failed")
    _candidate, date = _candidate_and_date(manifest)
    state_path = args.base / "state" / (date + ".json")
    with exclusive_lock(args.base / "runner.lock", label="published manual intake"):
        with _reconciliation_lock(args.base):
            if not (args.base / "DISABLED").is_file():
                raise ValueError("maintenance pause changed before intake")
            if state_path.is_symlink() or not state_path.is_file():
                raise ValueError("native daily state is not a regular file")
            before = state_path.read_bytes()
            state = json.loads(before)
            if already_applied_manual_intake(state, args.manifest, args.bvid):
                after, plan = state, {"status": "ALREADY_APPLIED", "state_changed": False}
            else:
                verified = verified_manual_inputs(args.manifest, args.bvid)
                after, plan = plan_manual_publication_intake(state, verified)
            payload = (json.dumps(after, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
            result = {"at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                      "run_mode": "APPLY" if args.apply else "DRY_RUN", "plan": plan,
                      "before_sha256": hashlib.sha256(before).hexdigest(),
                      "planned_after_sha256": hashlib.sha256(payload).hexdigest(),
                      "new_uploads": 0, "state_written": False}
            if args.apply and plan["state_changed"]:
                backup = args.receipt.with_suffix(".state-before.json")
                with backup.open("xb") as f:
                    f.write(before)
                    f.flush()
                    os.fsync(f.fileno())
                with args.receipt.with_suffix(".prepared.json").open("x") as f:
                    json.dump(result, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                if state_path.read_bytes() != before:
                    raise RuntimeError("daily state changed before intake CAS")
                atomic_write_bytes(state_path, payload)
                assert state_path.read_bytes() == payload
                result.update(state_written=True, backup_path=str(backup))
            with args.receipt.open("x") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
