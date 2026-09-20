#!/usr/bin/env python3
"""Bind an existing identity-card trial and actual V4 into a private audited video package."""

from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.audit_review_package import audit_package
from src.autoslice.host_only_identity_card_successor import build_identity_card_successor


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("source_package", type=Path)
    p.add_argument("--destination", type=Path, required=True)
    p.add_argument("--candidate", required=True)
    p.add_argument("--trial-result", type=Path, required=True)
    p.add_argument("--witness", type=Path, required=True)
    p.add_argument("--comparison", type=Path, required=True)
    a = p.parse_args()
    try:
        result = build_identity_card_successor(
            source_package=a.source_package,
            destination_package=a.destination,
            candidate_id=a.candidate,
            trial_result=a.trial_result,
            witness_receipt=a.witness,
            comparison_image=a.comparison,
            audit_package=audit_package,
        )
    except (ValueError, OSError) as exc:
        print(f"REFUSE: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
