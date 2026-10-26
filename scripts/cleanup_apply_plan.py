#!/usr/bin/env python3
"""Delete the files in a cleanup plan. The reviewed step after the preflight.

Acquires the existing runner commit lease through current qualification and
application. An embedding's live lease is reused. An external flock caller
must explicitly inherit its locked open-file descriptor with --runner-lock-fd;
an ancestor's open file observed by fuser is not a lock capability.

    bash -c '
      exec 9<>/opt/bilive/autoslice/runner.lock; flock -n 9 || exit 2
      python3 cleanup_preflight_scan.py --allow-source-extractions --json /tmp/plan.json &&
      python3 cleanup_apply_plan.py /tmp/plan.json --runner-lock-fd 9 --manifest /tmp/manifest.json'
"""
from __future__ import annotations

import argparse
import collections
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cleanup_preflight_scan import (
    ALLOWED_CLASSES, SOURCE_EXTRACTION_CLASSES, TERMINAL_STATES,
    authority_references, candidate_states, classify, eligibility_hold_reason,
    in_flight_candidates, owning_identity, owner_claim, quiet_window,
)
from _cleanup_file_identity import remove_if_matches, verify_preimage
checkout = str(Path(__file__).resolve().parents[1])
if checkout not in sys.path:
    sys.path.insert(0, checkout)
from src.autoslice import qixi_transaction_core as transaction_core


@contextmanager
def maintenance_exclusion(base: str, inherited_fd: int | None = None):
    """Use the existing mutex without reacquiring a live embedding/parent lock."""
    runtime = Path(base)
    current = transaction_core.current_runner_commit_lease(runtime_root=runtime)
    if current is not None:
        if inherited_fd is not None:
            raise ValueError("pass either the live embedding lease or an inherited lock FD")
        def verify_current():
            transaction_core.require_runner_commit_lease(current, runtime_root=runtime)
            transaction_core._safe_parent(current.lock_path)
            observed = os.lstat(current.lock_path)
            if (not stat.S_ISREG(observed.st_mode)
                    or (observed.st_dev, observed.st_ino) != (current.device, current.inode)):
                raise ValueError("runner lease pathname identity drifted")
        verify_current()
        yield verify_current
        return
    if inherited_fd is None:
        with transaction_core.exclusive_runner_commit(runtime):
            # Reuse the live accessor path to revalidate at qualification/use,
            # without obtaining a second flock on this inode.
            with maintenance_exclusion(base) as verify_current:
                yield verify_current
        return
    if type(inherited_fd) is not int or inherited_fd < 0:
        raise ValueError("inherited runner lock FD is invalid")
    lock_path = runtime / "runner.lock"
    transaction_core._safe_parent(lock_path)
    descriptor = os.dup(inherited_fd)
    try:
        opened = os.fstat(descriptor)
        transaction_core._same_safe_runner_lock(lock_path, descriptor, opened)
        # dup shares the external flock's open-file description. Reaffirming
        # that lock cannot deadlock; a different process's mutex refuses now.
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        def verify_inherited():
            transaction_core._same_safe_runner_lock(lock_path, descriptor, opened)
        verify_inherited()
        yield verify_inherited
    finally:
        # Never LOCK_UN a description whose lifetime belongs to the caller.
        os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", help="JSON plan produced by cleanup_preflight_scan.py")
    parser.add_argument("--base", default="/opt/bilive/autoslice")
    parser.add_argument("--manifest", help="write a cleanup-manifest.v1 fragment here")
    parser.add_argument("--max-plan-age-seconds", type=int, default=900)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--runner-lock-fd", type=int,
                        help="inherit the caller's locked runner.lock open-file descriptor")
    args = parser.parse_args()

    base = os.path.abspath(args.base)
    try:
        with maintenance_exclusion(base, args.runner_lock_fd) as verify_lock:
            reference_window = getattr(transaction_core, "_recovery_reference_window", None)
            combined_window = getattr(transaction_core, "exclusive_recovery_maintenance", None)
            if not callable(reference_window) or not callable(combined_window):
                raise ValueError("private recovery writer exclusion is not integrated")
            # State/journal writers use the runner mutex; concurrent private
            # copies use the shared side of A's reference mutex. Own both
            # exclusive sides before any qualification or target application.
            window = (combined_window(Path(base)) if args.runner_lock_fd is None
                      else reference_window(Path(base), maintenance=True))
            with window:
                return _apply_locked_plan(args, base, verify_lock)
    except (OSError, ValueError) as exc:
        print(f"ABORT: cleanup runner exclusion failed: {exc}. "
              "External flock callers must inherit --runner-lock-fd; do not reacquire an ancestor lock.",
              file=sys.stderr)
        return 2


def _apply_locked_plan(args, base: str, verify_lock) -> int:
    """Consume only the approved rows while the existing runner mutex is held."""

    problems = quiet_window(base)
    if problems:
        print("ABORT: host is not quiet at delete time:", file=sys.stderr)
        for problem in problems:
            print(f"   ! {problem}", file=sys.stderr)
        return 2

    try:
        age = time.time() - os.path.getmtime(args.plan)
    except OSError as exc:
        print(f"ABORT: cleanup plan cannot be read: {exc}", file=sys.stderr)
        return 2
    if age < 0 or args.max_plan_age_seconds < 0 or age > args.max_plan_age_seconds:
        print(f"ABORT: plan is {age:.0f}s old (limit {args.max_plan_age_seconds}s). "
              "Re-run the preflight inside this lock.", file=sys.stderr)
        return 2

    try:
        with open(args.plan, encoding="utf-8") as fh:
            plan = json.load(fh)
        if not isinstance(plan, list):
            raise ValueError("cleanup plan must be a list")
        seen = set()
        # Reject a stale/malformed batch before any target is removed. Each file
        # is checked again immediately before use below, under the same lock.
        for item in plan:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise ValueError("cleanup plan item has no path")
            if item["path"] in seen:
                raise ValueError("cleanup plan contains duplicate paths")
            seen.add(item["path"])
            actual_class = classify(os.path.basename(item["path"]))
            if (actual_class not in ALLOWED_CLASSES | SOURCE_EXTRACTION_CLASSES
                    or item.get("cls") != actual_class):
                raise ValueError("cleanup target is not in its declared allowed class")
            identity = owning_identity(base, item["path"])
            if (item.get("owner_identity") != owner_claim(identity)
                    or item.get("day") != identity[1] or item.get("owner") != identity[2]):
                raise ValueError("cleanup plan owner identity is missing or differs from its path; rebuild the plan")
            if (not isinstance(item.get("owner_status"), str)
                    or item["owner_status"] not in TERMINAL_STATES):
                raise ValueError("cleanup plan lacks the original terminal-owner claim")
            preimage = item.get("preimage")
            verify_preimage(base, item["path"], preimage)
            if type(item.get("bytes")) is not int or item["bytes"] != preimage["bytes"]:
                raise ValueError("cleanup plan size differs from its preimage")
            if any(not isinstance(item.get(k), str) for k in ("day", "cls")):
                raise ValueError("cleanup plan reporting fields are invalid")
    except (OSError, ValueError) as exc:
        print(f"ABORT: cleanup preimage verification failed: {exc}", file=sys.stderr)
        return 2
    if not plan:
        print("plan is empty; nothing to delete")
        return 0

    try:
        refs, refdirs = authority_references(base)
        status, pending = candidate_states(base, strict=True)
        in_flight = in_flight_candidates(base)
        # Check the entire original set before the first remove; never create
        # a fresh deletion set or silently drop a newly protected member.
        for item in plan:
            identity = owning_identity(base, item["path"])
            reason = eligibility_hold_reason(base, item["path"], refs, refdirs, status, pending, in_flight)
            if reason:
                raise ValueError(f"{reason}: {item['path']}")
            if status.get(identity) != item["owner_status"]:
                raise ValueError(f"original owner status drifted: {item['path']}")
        verify_lock()
    except (OSError, ValueError) as exc:
        print(f"ABORT: current cleanup qualification failed: {exc}", file=sys.stderr)
        return 2

    deleted, freed = [], 0
    skipped: collections.Counter = collections.Counter()
    for item in plan:
        try:
            verify_lock()
            remove_if_matches(base, item["path"], item["preimage"], dry_run=args.dry_run)
        except (OSError, ValueError) as exc:
            print(f"ABORT: cleanup target changed: {exc}", file=sys.stderr)
            skipped["file_identity_changed_at_application"] += 1
            break
        deleted.append(item)
        freed += item["bytes"]

    by_day: collections.Counter = collections.Counter()
    by_class: collections.Counter = collections.Counter()
    for item in deleted:
        by_day[item["day"]] += item["bytes"]
        by_class[item["cls"]] += item["bytes"]

    verb = "would delete" if args.dry_run else "deleted"
    print(f"{verb} {len(deleted)} files, {freed / 2**30:.2f} GiB")
    for day, size in sorted(by_day.items()):
        print(f"   {day:13} {size / 2**30:8.2f} GiB")
    for reason, count in skipped.most_common():
        print(f"   SKIPPED {count}: {reason}")

    if args.manifest:
        with open(args.manifest, "w", encoding="utf-8") as fh:
            json.dump({
                "deleted_files": 0 if args.dry_run else len(deleted),
                "would_delete_files": len(deleted) if args.dry_run else 0,
                "freed_bytes": 0 if args.dry_run else freed,
                "would_delete_logical_bytes": freed if args.dry_run else 0,
                "freed_bytes_basis": "SUM_LOGICAL_BYTES_NOT_MEASURED",
                "by_day": dict(by_day),
                "by_class": dict(by_class),
                "skipped": dict(skipped),
                "dry_run": args.dry_run,
                "paths": [item["path"] for item in deleted],
            }, fh, ensure_ascii=False, indent=1)
        print(f"wrote {args.manifest}")
    return 2 if skipped else 0


if __name__ == "__main__":
    raise SystemExit(main())
