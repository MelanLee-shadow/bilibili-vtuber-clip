#!/usr/bin/env python3
"""Build deterministic HOST_ONLY identity-card cover pixels without binding a package."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.host_only_identity_card_trial import (  # noqa: E402
    HostOnlyIdentityCardTrialError,
    build_host_only_identity_card_trial,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_package", type=Path)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument(
        "--title-exclusion-authority",
        type=Path,
        help="Existing candidate/current-poster-bound root visual title-placement review",
    )
    args = parser.parse_args()
    try:
        result = build_host_only_identity_card_trial(
            source_package=args.source_package,
            destination=args.destination,
            candidate_id=args.candidate,
            title_exclusion_authority=args.title_exclusion_authority,
        )
    except (HostOnlyIdentityCardTrialError, OSError) as exc:
        print(f"REFUSE: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
