#!/usr/bin/env python3
"""Delete the files in a cleanup plan. The reviewed step after the preflight.

Intended to run inside the same `flock runner.lock` as the preflight that
produced the plan — holding the lock is what stops the next cron tick from
starting, and it bounds how stale the plan can be. A plan older than the age
limit is refused rather than trusted: the runner writes into out/<today>, so a
plan from an hour ago may name files a live tick has since re-created.

    flock -w 3600 /opt/bilive/autoslice/runner.lock bash -c '
      python3 cleanup_preflight_scan.py --allow-source-extractions --json /tmp/plan.json &&
      python3 cleanup_apply_plan.py /tmp/plan.json --manifest /tmp/manifest.json'
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cleanup_preflight_scan import (  # noqa: E402
    ALLOWED_CLASSES, SOURCE_EXTRACTION_CLASSES, TERMINAL_STATES,
    classify, owning_ids, quiet_window,
)
from _cleanup_file_identity import remove_if_matches, verify_preimage  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", help="JSON plan produced by cleanup_preflight_scan.py")
    parser.add_argument("--base", default="/opt/bilive/autoslice")
    parser.add_argument("--manifest", help="write a cleanup-manifest.v1 fragment here")
    parser.add_argument("--max-plan-age-seconds", type=int, default=900)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    base = args.base.rstrip("/")

    problems = quiet_window(base)
    if problems:
        print("ABORT: host is not quiet at delete time:", file=sys.stderr)
        for problem in problems:
            print(f"   ! {problem}", file=sys.stderr)
        return 2

    age = time.time() - os.path.getmtime(args.plan)
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
            ids = owning_ids(item["path"])
            if not ids or item.get("owner") not in ids or item.get("owner_status") not in TERMINAL_STATES:
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

    deleted, freed = [], 0
    skipped: collections.Counter = collections.Counter()
    for item in plan:
        try:
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
