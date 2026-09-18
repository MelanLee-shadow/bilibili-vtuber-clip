#!/usr/bin/env python3
"""Build and audit one create-only HOST_ONLY v4 review-package successor."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_review_package import audit_package  # noqa: E402
from src.autoslice.host_only_v4_successor import (  # noqa: E402
    HostOnlyV4SuccessorError,
    build_host_only_v4_successor,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_package", type=Path)
    parser.add_argument("--destination-package", type=Path, required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--witness-receipt", type=Path, required=True)
    parser.add_argument("--comparison-image", type=Path, required=True)
    parser.add_argument("--provider-receipt", type=Path)
    args = parser.parse_args()
    try:
        result = build_host_only_v4_successor(
            source_package=args.source_package,
            destination_package=args.destination_package,
            candidate_id=args.candidate,
            witness_receipt=args.witness_receipt,
            comparison_image=args.comparison_image,
            provider_receipt=args.provider_receipt,
            audit_package=audit_package,
        )
    except HostOnlyV4SuccessorError as exc:
        print(f"REFUSE: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
