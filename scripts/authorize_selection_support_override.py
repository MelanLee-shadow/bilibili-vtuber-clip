#!/usr/bin/env python3
"""Dry-run or create one no-upload terminal selection-support authority."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.producer_delivery_transaction import deployment_authority_binding  # noqa: E402
from src.autoslice.selection_support_override import (  # noqa: E402
    SelectionSupportOverrideError,
    _safe_document,
    create_or_apply_selection_support_override,
    prepare_selection_support_override,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--date", required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--expires-at", required=True)
    parser.add_argument("--expected-state-sha256", required=True)
    parser.add_argument("--expected-deployed-commit", required=True)
    parser.add_argument("--expected-authority-manifest-sha256", required=True)
    parser.add_argument("--authorization-source-path", required=True)
    parser.add_argument("--authorization-source-sha256", required=True)
    parser.add_argument("--authorization-event-id", required=True)
    parser.add_argument("--authorization-user-id", required=True)
    parser.add_argument("--authorization-timestamp", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    authority = deployment_authority_binding(args.runtime_root)
    if (
        authority["commit"] != args.expected_deployed_commit
        or authority["authority_manifest_sha256"] != args.expected_authority_manifest_sha256
    ):
        print("SELECTION_SUPPORT_OVERRIDE_DEPLOYMENT_DRIFT", file=sys.stderr)
        return 2
    authorization = {
        "source_path": args.authorization_source_path,
        "source_sha256": args.authorization_source_sha256,
        "event_id": args.authorization_event_id,
        "user_id": args.authorization_user_id,
        "timestamp": args.authorization_timestamp,
        "scope": "SELECTION_SUPPORT_ONLY",
    }
    try:
        path, document = prepare_selection_support_override(
            runtime_root=args.runtime_root,
            date=args.date,
            candidate_id=args.candidate_id,
            nonce=args.nonce,
            expires_at=args.expires_at,
            expected_state_sha256=args.expected_state_sha256,
            expected_authority=authority,
            authorization=authorization,
        )
        if args.apply:
            create_or_apply_selection_support_override(path, document, runtime_root=args.runtime_root)
            document = _safe_document(path)
        print(json.dumps({"dry_run": not args.apply, "authority_path": str(path), "authority": document}, ensure_ascii=False, sort_keys=True))
        return 0
    except (SelectionSupportOverrideError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
