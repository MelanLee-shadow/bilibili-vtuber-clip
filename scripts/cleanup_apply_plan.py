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
from cleanup_preflight_scan import quiet_window  # noqa: E402


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
    if age > args.max_plan_age_seconds:
        print(f"ABORT: plan is {age:.0f}s old (limit {args.max_plan_age_seconds}s). "
              "Re-run the preflight inside this lock.", file=sys.stderr)
        return 2

    with open(args.plan, encoding="utf-8") as fh:
        plan = json.load(fh)
    if not plan:
        print("plan is empty; nothing to delete")
        return 0

    deleted, freed = [], 0
    skipped: collections.Counter = collections.Counter()
    for item in plan:
        path, expected = item["path"], int(item["bytes"])
        try:
            stat = os.stat(path)
        except OSError:
            skipped["vanished_since_plan"] += 1
            continue
        if stat.st_size != expected:
            # A size change means the file was rewritten after the scan — the
            # plan no longer describes what is on disk, so leave it alone.
            skipped["size_changed_since_plan"] += 1
            continue
        if args.dry_run:
            deleted.append(item)
            freed += expected
            continue
        try:
            os.unlink(path)
        except OSError as exc:
            skipped[f"unlink_failed:{exc.errno}"] += 1
            continue
        deleted.append(item)
        freed += expected

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
                "deleted_files": len(deleted),
                "freed_bytes": freed,
                "by_day": dict(by_day),
                "by_class": dict(by_class),
                "skipped": dict(skipped),
                "dry_run": args.dry_run,
                "paths": [item["path"] for item in deleted],
            }, fh, ensure_ascii=False, indent=1)
        print(f"wrote {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
