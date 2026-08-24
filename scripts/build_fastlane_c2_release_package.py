#!/usr/bin/env python3
"""Create a private, ordinary same-stem C2 new-BV package.

This is a package projection only.  It cannot create an upload manifest,
call CPA title/cover QC, modify registry/runtime state, or upload media.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_lidousha_review_package import audit_package
from src.autoslice.fastlane_c2_release_bridge import (
    C2ReleaseBridgeError,
    _write_create_only_json,
    build_release_package,
    sha256,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--formal-package", type=Path, required=True)
    parser.add_argument("--ready-proposal", type=Path, required=True)
    parser.add_argument("--root-receipt", type=Path, required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--legacy-proposal", type=Path, required=True)
    parser.add_argument("--legacy-execution-contract", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        out = build_release_package(
            formal_package=args.formal_package,
            ready_proposal=args.ready_proposal,
            root_receipt=args.root_receipt,
            authorization=args.authorization,
            legacy_proposal=args.legacy_proposal,
            legacy_execution_contract=args.legacy_execution_contract,
            out=args.out,
        )
        audit = audit_package(out)
        if audit.get("passed") is not True or audit.get("blocking_issue_count") != 0:
            raise C2ReleaseBridgeError("current C2 release package audit did not pass")
        audit_path = out / "package_audit.json"
        _write_create_only_json(audit_path, audit)
    except C2ReleaseBridgeError as exc:
        print(f"REFUSE: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"package": str(out), "package_audit_sha256": sha256(audit_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
