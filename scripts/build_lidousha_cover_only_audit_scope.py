#!/usr/bin/env python3
"""Create one explicit, predecessor-bound cover-only audit scope."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.cover_only_audit_scope import (
    CoverOnlyAuditScopeError,
    create_scope,
    write_scope_create_only,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Bind a cover-only same-BV package to a completed predecessor "
            "without weakening current cover QC"
        )
    )
    parser.add_argument("package_root", type=Path)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument(
        "--predecessor-completed", type=Path, required=True
    )
    parser.add_argument("--authorized-by", default="Ivan")
    parser.add_argument("--authorization-quote", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        scope = create_scope(
            package_root=args.package_root,
            candidate_id=args.candidate_id,
            predecessor_completed_path=args.predecessor_completed,
            authorized_by=args.authorized_by,
            authorization_quote=args.authorization_quote,
        )
        write_scope_create_only(args.out, scope)
    except (CoverOnlyAuditScopeError, OSError) as exc:
        print(f"REFUSE: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "scope": str(args.out.resolve()),
                "scope_sha256": scope["scope_sha256"],
                "candidate_id": scope["candidate_id"],
                "bvid": scope["publication_target"]["bvid"],
                "current_cid": scope["publication_target"]["current_cid"],
                "reused_gate": scope["reused_gate"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
