#!/usr/bin/env python3
"""Produce an offline timing ledger and readable summary; no provider/runtime imports."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.run_time_ledger import build_ledger, collect_sources, render_summary, write_ledger


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--log", type=Path, action="append", default=[])
    parser.add_argument("--artifact-manifest", type=Path, action="append", default=[])
    parser.add_argument("--start", help="UTC ISO timestamp; use to isolate a recovery in shared archives")
    parser.add_argument("--end", help="UTC ISO timestamp; snapshot time for an unfinished run")
    parser.add_argument("--cid")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args(argv)
    if not args.run_dir.is_dir():
        parser.error("run_dir must be an existing directory")
    try:
        ledger = build_ledger(collect_sources(args.run_dir, log_paths=args.log,
                              artifact_manifests=args.artifact_manifest),
                              start=args.start, end=args.end, candidate_id=args.cid)
    except ValueError as exc:
        parser.error(str(exc))
    write_ledger(args.output, ledger)
    summary = render_summary(ledger)
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(summary, encoding="utf-8")
    print(summary, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
