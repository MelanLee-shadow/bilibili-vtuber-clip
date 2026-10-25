#!/usr/bin/env python3
"""Prepare the exact reviewed B2 v7 package; never deploy, upload or replace it.

An accepted E428 package already exists. This entry makes its implementation
available in normal source releases, not an instruction to recreate that package.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true", required=True)
    parser.add_argument("--source-package", type=Path, required=True,
                        help="Existing exact reviewed a8 package; frozen evidence hashes must match")
    parser.add_argument("--output", type=Path, required=True,
                        help="Absent release-v7-e428-a1 under an existing dedicated receipt directory")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # Help and argument failures do not import pipeline/runtime dependencies.
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from src.autoslice.b2_v7_recovery_successor import prepare

    try:
        result = prepare(args.source_package, args.output)
    except Exception as exc:
        # Preserve any partial namespace; never recreate or delete it on failure.
        print(json.dumps({"status": "FAILED_CLOSED", "error_type": type(exc).__name__,
                          "error": str(exc), "production_adopted": False,
                          "upload_performed": False}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"].startswith("PRIVATE_PACKAGE_AUDIT_PASS") else 2


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
