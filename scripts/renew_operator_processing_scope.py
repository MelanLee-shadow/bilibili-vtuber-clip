#!/usr/bin/env python3
"""Dry-run-first renewal for an existing v2 operator processing scope.

This command never starts the runner.  ``--apply`` is the sole state mutation
and is protected by the canonical tick.lock -> RunnerCommitLease order.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.historical_fastlane_authority import (  # noqa: E402
    HistoricalFastlaneAuthorityError,
    commit_scope_renewal,
    prepare_scope_renewal,
)
from src.autoslice.producer_delivery_transaction import deployment_authority_binding  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--date", required=True)
    parser.add_argument("--candidate-id", action="append", required=True)
    parser.add_argument("--grant-id", required=True)
    parser.add_argument("--expires-at", required=True)
    parser.add_argument("--expected-state-sha256", required=True)
    parser.add_argument("--expected-deployed-commit", required=True)
    parser.add_argument("--expected-authority-manifest-sha256", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    expected = deployment_authority_binding(args.runtime_root)
    if (
        expected["commit"] != args.expected_deployed_commit
        or expected["authority_manifest_sha256"] != args.expected_authority_manifest_sha256
    ):
        print("HISTORICAL_FASTLANE_DEPLOYMENT_DRIFT", file=sys.stderr)
        return 2
    try:
        prepared = prepare_scope_renewal(
            runtime_root=args.runtime_root, date=args.date,
            candidate_ids=tuple(args.candidate_id), new_grant_id=args.grant_id,
            expires_at=args.expires_at, expected_state_sha256=args.expected_state_sha256,
            expected_authority=expected,
        )
        output = {"dry_run": not args.apply, "receipt": prepared.receipt, "receipt_path": str(prepared.receipt_path)}
        if args.apply:
            output["receipt_path"] = str(commit_scope_renewal(prepared, runtime_root=args.runtime_root))
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 0
    except HistoricalFastlaneAuthorityError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
