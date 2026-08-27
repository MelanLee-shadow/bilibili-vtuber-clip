#!/usr/bin/env python3
"""Emit the correction-scope plan for one operator report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.operator_correction_policy import plan_operator_correction


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--issue-count", required=True, type=int)
    parser.add_argument("--explicitly-exhaustive", action="store_true")
    parser.add_argument(
        "--only-these-errors",
        action="store_true",
        help="the operator explicitly states that the listed errors are complete",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            plan_operator_correction(
                candidate_id=args.candidate,
                issue_count=args.issue_count,
                explicitly_exhaustive=args.explicitly_exhaustive,
                only_these_errors=args.only_these_errors,
            ),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
